"""Deterministic MariaDB faults injected INSIDE the durable worker path of a
real create_user (receipt approval, required), at the DBAPI level - the
exception the driver itself raises, wrapped by SQLAlchemy as in production.

  * 1213 (deadlock): InnoDB rolls the whole transaction back - emulated by
    rolling the connection back before raising.
  * 1205 (lock wait timeout): with innodb_rollback_on_timeout OFF (the
    default) MariaDB rolls back only the STATEMENT; the transaction stays
    open with its earlier writes. Shown with a real lock held by another
    transaction, and injected inside T_final with the earlier writes of the
    transaction still pending.
  * an unrelated error (lost connection 2013, a programming error) is never
    taken for a lock conflict: it reaches the caller. So does a conflict in
    T1 itself, which runs before any worker iteration; nothing of it is kept
    and the same approval sent again completes.

After every fault: the operation is resumable and completes; one customer,
one Purchase, one sale, one card payment, one effect per manifest row; no
remote write repeated (a write whose result was lost is verified by a
recovery read, never written again); the next session is healthy.

Not reproduced here: the original CI deadlock itself (its lock pair is
unknown); these faults are placed where a lock conflict can occur.

Run:  MARIADB_TEST_URL=... python3 backend/tests/test_durable_fault_injection.py
"""
from __future__ import annotations

import contextlib
import os
import re
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-fault-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

import pymysql
from sqlalchemy import event, func, select, text

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_parent_execute as execute, remote_action as action
import _receipt_void_env as fixture

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def normal(statement):
    return re.sub(r"\s+", " ", statement).strip()


TICK_STEP_LOCK = lambda s: (s.startswith("SELECT provisioning_steps.id") and "FOR UPDATE" in s
                            and "ORDER BY provisioning_steps.step_order" in s)
RESULT_RECORD = lambda s: s.startswith("UPDATE provisioning_steps SET state=")
FINAL_LEDGER = lambda s: s.startswith("INSERT INTO ledger_entries")


def scenario(engine):
    with contextlib.ExitStack() as stack:
        env = fixture.environment("faults", engine, stack)
        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1))
            db.commit()
        remote = {"writes": 0, "recovery_reads": 0}

        def runner(dto):
            if dto.action_type is action.ActionType.WG_ENSURE_PRESENT:
                remote["recovery_reads" if (dto.params or {}).get("recovery_read") else "writes"] += 1
            return fixture.runner(dto)
        stack.enter_context(patch.object(execute.remote_runner, "run_action", side_effect=runner))

        plan = {}

        @event.listens_for(engine, "do_execute")
        def inject(cursor, statement, parameters, context):
            fault = plan.get("fault")
            if not fault or plan.get("fired"):
                return None
            s = normal(statement)
            if fault.get("after_remote") and remote["writes"] + remote["recovery_reads"] == plan["remote_base"]:
                return None
            if not fault["where"](s):
                return None
            plan["fired"] = True
            kind = fault["kind"]
            if kind == "deadlock":
                cursor.connection.rollback()              # InnoDB: the whole transaction is gone
                raise pymysql.err.OperationalError(1213, "Deadlock found when trying to get lock; "
                                                         "try restarting transaction")
            if kind == "lock_wait_statement_only":
                cursor.execute("SELECT trx_rows_modified FROM information_schema.innodb_trx "
                               "WHERE trx_mysql_thread_id = CONNECTION_ID()")
                plan["pending_writes"] = (cursor.fetchone() or (0,))[0]
                raise pymysql.err.OperationalError(1205, "Lock wait timeout exceeded; try restarting transaction")
            if kind == "real_lock_wait":
                operation_id = parameters.get("operation_id_1")
                blocker = engine.connect()
                blocker.execute(text("SELECT id FROM provisioning_steps WHERE operation_id = :o FOR UPDATE"),
                                {"o": operation_id})
                cursor.execute("SET SESSION innodb_lock_wait_timeout = 1")
                cursor.execute("SELECT trx_rows_modified FROM information_schema.innodb_trx "
                               "WHERE trx_mysql_thread_id = CONNECTION_ID()")
                plan["pending_writes"] = (cursor.fetchone() or (0,))[0]
                try:
                    cursor.execute(statement, parameters)
                except pymysql.err.OperationalError as exc:
                    plan["real_code"] = exc.args[0]
                    cursor.execute("SELECT trx_rows_modified FROM information_schema.innodb_trx "
                                   "WHERE trx_mysql_thread_id = CONNECTION_ID()")
                    plan["still_open_writes"] = (cursor.fetchone() or (0,))[0]
                    raise
                finally:
                    cursor.execute("SET SESSION innodb_lock_wait_timeout = 50")
                    blocker.rollback()
                    blocker.close()
                return True
            if kind == "lost_connection":
                cursor.connection.rollback()
                raise pymysql.err.OperationalError(2013, "Lost connection to MySQL server during query")
            if kind == "programming_error":
                raise KeyError("injected programming error")
            raise AssertionError(kind)

        def state_of(username, item):
            with env.Session() as db:
                user = db.query(models.User).filter_by(username=username).one_or_none()
                operation = db.execute(select(mp.ProvisioningOperation.state).where(
                    mp.ProvisioningOperation.approval_uuid == item["approval_uuid"])).scalars().all()
                return dict(
                    users=db.query(func.count(models.User.id)).filter_by(username=username).scalar(),
                    purchases=db.query(func.count(models.Purchase.id)).filter_by(user_id=user.id).scalar() if user else 0,
                    sales=db.query(func.count(models.LedgerEntry.id)).filter(
                        models.LedgerEntry.username_snapshot == username, models.LedgerEntry.kind == "sale_new").scalar(),
                    effects=db.query(func.count(rv.receipt_approval_effects.c.id)).filter(
                        rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"]).scalar(),
                    expected=db.query(func.count(rv.receipt_approval_expected_effects.c.id)).filter(
                        rv.receipt_approval_expected_effects.c.approval_uuid == item["approval_uuid"],
                        rv.receipt_approval_expected_effects.c.requirement == "required").scalar(),
                    approval=env.approval(item["approval_uuid"])["state"], operations=operation)

        def exactly_once(label, got, writes, recovery_reads=0):
            check(label + ": completed, exactly once everywhere (as without a fault)",
                  {k: v for k, v in got.items() if k != "expected"} | {"all_effects": got["effects"] == got["expected"]},
                  baseline)
            check(label + ": the remote write happened once" + (", then was verified, not repeated"
                                                                if recovery_reads else ""),
                  (remote["writes"] - writes[0], remote["recovery_reads"] - writes[1]), (1, recovery_reads))

        def healthy(label):
            with env.Session() as db:
                check(label + ": the next session is healthy (no transaction left open)",
                      (db.execute(text("SELECT 1")).scalar(), db.execute(text("SELECT @@in_transaction")).scalar()),
                      (1, 0))

        cases = [
            ("1213 on the worker's step lock (before the remote call)",
             dict(kind="deadlock", where=TICK_STEP_LOCK), 0),
            ("1213 while recording the remote result (after the remote write)",
             dict(kind="deadlock", where=RESULT_RECORD, after_remote=True), 1),
            ("1213 inside T_final (the sale insert)", dict(kind="deadlock", where=FINAL_LEDGER), 0),
            ("1205 inside T_final, statement-only rollback (MariaDB default)",
             dict(kind="lock_wait_statement_only", where=FINAL_LEDGER), 0),
            ("a real 1205: another transaction holds the operation's steps",
             dict(kind="real_lock_wait", where=TICK_STEP_LOCK), 0),
        ]
        with env.Session() as db:
            rollback_on_timeout = db.execute(text("SELECT @@innodb_rollback_on_timeout")).scalar()
        check("this server rolls back only the statement on a lock wait timeout (innodb_rollback_on_timeout OFF)",
              int(rollback_on_timeout), 0)
        base = (remote["writes"], remote["recovery_reads"])
        item = env.new_user("nofault", 9299)
        got = state_of("nofault", item)
        baseline = {k: v for k, v in got.items() if k != "expected"} | {"all_effects": got["effects"] == got["expected"]}
        check("without a fault: completed, one customer, Purchase, sale and every manifest effect",
              baseline, dict(users=1, purchases=1, sales=1, effects=got["expected"], approval="completed",
                             operations=["completed"], all_effects=True))
        baseline["effects"] = got["effects"]
        check("...one remote write", (remote["writes"] - base[0], remote["recovery_reads"] - base[1]), (1, 0))
        for n, (label, fault, recovery_reads) in enumerate(cases):
            plan.clear()
            plan["fault"] = fault
            plan["remote_base"] = remote["writes"] + remote["recovery_reads"]
            before = (remote["writes"], remote["recovery_reads"])
            item = env.new_user(f"fault{n}", 9300 + n)
            check(label + ": the fault really fired", plan.get("fired"), True)
            if fault["kind"] == "real_lock_wait":
                check(label + ": MariaDB answered 1205", plan.get("real_code"), 1205)
            if fault["kind"] in ("lock_wait_statement_only", "real_lock_wait"):
                check(label + ": the failed transaction still held its earlier writes after the error "
                      "(so the Session's rollback, not MariaDB, discards them)",
                      (plan.get("still_open_writes", plan.get("pending_writes")) or 0) > 0, True)
            exactly_once(label, state_of(f"fault{n}", item), before, recovery_reads)
            healthy(label)

        T1_INSERT = lambda s: s.startswith("INSERT INTO provisioning_operations")
        for n, (label, kind, where, raises) in enumerate((
                ("an unrelated OperationalError (2013 lost connection) in T_final", "lost_connection", FINAL_LEDGER,
                 "OperationalError"),
                ("a programming error in T_final", "programming_error", FINAL_LEDGER, "KeyError"),
                # T1 runs before the worker iteration (start_once): its own
                # conflict is not retried there - the request fails, nothing
                # is kept, and the same approval is simply sent again.
                ("1213 in T1 (before any worker iteration)", "deadlock", T1_INSERT, "OperationalError")), start=10):
            plan.clear()
            plan["fault"] = dict(kind=kind, where=where)
            plan["remote_base"] = remote["writes"] + remote["recovery_reads"]
            before = (remote["writes"], remote["recovery_reads"])
            try:
                item = env.new_user(f"fault{n}", 9300 + n)
                raised = None
            except Exception as exc:  # noqa: BLE001
                raised = type(exc).__name__
            check(label + ": reaches the caller, never hidden as a retry", raised, raises)
            with env.Session() as db:
                approval_uuid = db.execute(select(rv.receipt_approvals.c.approval_uuid).where(
                    rv.receipt_approvals.c.target_username_snapshot == f"fault{n}")).scalar()
                partial = (db.query(func.count(models.User.id)).filter_by(username=f"fault{n}").scalar(),
                           db.query(func.count(models.LedgerEntry.id)).filter(
                               models.LedgerEntry.username_snapshot == f"fault{n}").scalar())
            check(label + ": nothing of the failed transaction is kept", partial, (0, 0))
            healthy(label)
            plan.clear()
            item = {"approval_uuid": approval_uuid}
            with env.Session() as db:
                token = env.approval(approval_uuid)["version"]
            from app import schemas
            from app.routers import bot as bot_router
            env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
                username=f"fault{n}", telegram_id=9300 + n, package_id=env.ids.shared, paid_amount=1000,
                payment_method="card", approval_uuid=approval_uuid, execution_token=token, list_price=1000),
                db=db, principal=env.internal))
            exactly_once(label + ", then the same approval again", state_of(f"fault{n}", item), before)


with contextlib.ExitStack() as cleanup:
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        scenario(maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  MariaDB fault injection (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
