"""The renewal void's hold against a consumer or a renewal that checked it
just BEFORE the hold committed (review of 4b31a13; fails there). Two real
Sessions, a deterministic stop exactly between the consumer's check and its
commit. SQLite and real MariaDB; fake runner, no network.

  hold first   the poller (and RADIUS) checked "not held", the block step
               commits its hold, the poller commits: the queue is NOT
               consumed, no consumption history is left, the void completes;
  consume first the poller commits its consumption before the block step:
               the block step sees the new state and refuses before any
               financial reversal (renewal_state_changed);
  renewal      a renewal that checked "not held" and commits after the hold:
               refused at its commit (409 purchase_void_in_progress), nothing
               of it stays;
  node calls   the consumption's node reconciliation runs after the commit,
               with SQLite's writer free.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-void-hold-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import models, models_reservation_transition as rt, schemas
from app.routers import bot as bot_router
from app.services import receipt_void_engine as void_engine, receipt_void_runner as runner
from app.services import reservation_transition, user_ops
import _receipt_void_env as fixture

GB = 1024 ** 3
failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    mariadb = engine.dialect.name != "sqlite"
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        Session = env.Session
        tgs = iter(range(8700, 8999))

        def customer_with_queue():
            tg = next(tgs)
            name = f"vh{tg}"
            env.new_user(name, tg)
            with Session() as db:
                user = db.query(models.User).filter_by(username=name).one()
                pid = db.query(models.Purchase).filter_by(user_id=user.id).one().id
            item = env.begin("renew", name, 1000, package_id=env.ids.shared, renew_purchase_id=pid)
            env.run(lambda db: bot_router.renew_service(name, pid, schemas.BotRenewRequest(
                add_gb=10, add_days=30, package_id=env.ids.shared, paid_amount=1000, payment_method="card",
                approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=1000),
                db=db, owner_admin_id=None, principal=env.internal))
            return name, pid, item["approval_uuid"]

        def claim(approval_uuid, name):
            shown = env.preview(approval_uuid)
            with patch.object(runner, "advance", return_value="running"):      # claimed only; block pending
                return void_engine.execute(Session, env.root, preview_token=shown["preview_token"], typed_username=name,
                                           reason="fake receipt", danger_ack=True, ip="203.0.113.9")["operation_id"]

        def holds(approval_uuid):
            with Session() as db:
                return [r.state for r in db.execute(select(rt.purchase_void_holds.c.state).where(
                    rt.purchase_void_holds.c.approval_uuid == approval_uuid))]

        def queue(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return p.reserved_quota_bytes, int(p.reservation_activation_seq or 0), int(p.quota_bytes or 0)

        def logged(pid):
            with Session() as db:
                return db.execute(select(func.count()).select_from(rt.purchase_reservation_activations).where(
                    rt.purchase_reservation_activations.c.purchase_id == pid)).scalar()

        def finish(op):
            for _ in range(12):
                runner.advance(Session, op, budget=0.0)
            return env.steps_of(op)

        def stopped_consumer(pid, trigger, checked, go, out, probes=None):
            """The poller / RADIUS consumption, stopped right after it checked
            the hold (inside lock_for_consumption), resumed on `go`."""
            real = reservation_transition.is_held

            def is_held(db, key):
                answer = real(db, key)
                if threading.current_thread().name == "consumer":
                    checked.append(answer)
                    go.wait(30)
                return answer

            def run():
                with patch.object(reservation_transition, "is_held", side_effect=is_held):
                    with Session() as db:
                        p = db.get(models.Purchase, pid)
                        p.used_bytes = int(p.quota_bytes or 0)              # the term ran out
                        try:
                            out["applied"] = user_ops._maybe_activate_reserved_purchase_renewal(db, p, trigger)
                            db.commit()
                        except OperationalError as exc:                      # MariaDB snapshot isolation
                            db.rollback()
                            out["applied"] = f"restart:{exc.orig.args[0]}"
            return threading.Thread(target=run, name="consumer")

        # ---------------------------------------------------------------- the hold commits first
        for trigger in ("poller", "radius"):
            name, pid, approval = customer_with_queue()
            op = claim(approval, name)
            checked, go, out = [], threading.Event(), {}
            consumer = stopped_consumer(pid, trigger, checked, go, out)
            consumer.start()
            for _ in range(300):
                if checked:
                    break
                threading.Event().wait(0.02)
            check(L + f"{trigger}: the consumer checked the hold before it existed", checked, [False])
            if mariadb:
                # MariaDB: the consumer holds the purchase row lock; the block
                # step waits for it - run it after the consumer instead.
                go.set()
                consumer.join(60)
                runner.advance(Session, op, budget=0.0)
            else:
                runner.advance(Session, op, budget=0.0)                     # the block step commits its hold
                check(L + f"{trigger}: the block step committed its hold", holds(approval), ["active"])
                go.set()
                consumer.join(60)
            after_commit = (out.get("applied"), queue(pid)[0], queue(pid)[1], logged(pid))
            steps = finish(op)
            if mariadb:
                check(L + f"{trigger}: MariaDB serialises on the row: the consumption first, then the block step "
                      "refuses before any reversal", (out["applied"], steps["block"][0], steps["ledger_reversal"][0]),
                      (True, "failed", "pending"))
                continue
            check(L + f"{trigger}: its commit after the hold: NOT consumed, no consumption history",
                  after_commit[1:], (10 * GB, 0, 0))
            check(L + f"{trigger}: the void completes; the queue is gone with it",
                  (all(state == "done" for state, _ in steps.values()), queue(pid)[0], holds(approval)),
                  (True, None, ["released"]))

        # ---------------------------------------------------------------- the consumption commits first
        name, pid, approval = customer_with_queue()
        op = claim(approval, name)
        with Session() as db:
            p = db.get(models.Purchase, pid)
            p.used_bytes = int(p.quota_bytes or 0)
            check(L + "consume first: the queue starts", user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller"),
                  True)
            db.commit()
        steps = finish(op)
        check(L + "...the block step sees the new state and refuses before any financial reversal",
              (steps["block"][0], steps["ledger_reversal"][0], holds(approval)), ("failed", "pending", []))

        # ---------------------------------------------------------------- a renewal checked before the hold
        if not mariadb:
            name, pid, approval = customer_with_queue()
            op = claim(approval, name)
            checked, go, out = [], threading.Event(), {}
            real = reservation_transition.is_held

            def held_then_wait(db, key):
                answer = real(db, key)
                if threading.current_thread().name == "renewer":
                    checked.append(answer)
                    go.wait(30)
                return answer

            def renewal():
                with patch.object(reservation_transition, "is_held", side_effect=held_then_wait):
                    with Session() as db:
                        try:
                            user_ops.renew_purchase(db, db.get(models.Purchase, pid), add_gb=1, add_days=1)
                            out["r"] = "renewed"
                        except HTTPException as exc:
                            db.rollback()
                            out["r"] = exc.detail
            t = threading.Thread(target=renewal, name="renewer")
            t.start()
            for _ in range(300):
                if checked:
                    break
                threading.Event().wait(0.02)
            runner.advance(Session, op, budget=0.0)
            go.set()
            t.join(60)
            with Session() as db:
                contributions = db.execute(select(func.count()).select_from(rt.purchase_renewal_contributions).where(
                    rt.purchase_renewal_contributions.c.purchase_id == pid)).scalar()
            check(L + "a renewal that checked before the hold: refused at its commit, nothing of it stays",
                  (checked, out.get("r"), queue(pid)[0], contributions), ([False], "purchase_void_in_progress", 10 * GB, 1))
            finish(op)

            # the consumption's node reconciliation runs after its commit, the writer free
            import sqlite3
            name, pid, approval = customer_with_queue()
            probes = []

            def probe(_db, _purchase):
                other = sqlite3.connect(engine.url.database, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                    probes.append("free")
                except sqlite3.OperationalError:
                    probes.append("locked")
                finally:
                    other.close()
            with patch.object(user_ops, "reconcile_purchase_connections", side_effect=probe):
                with Session() as db:
                    p = db.get(models.Purchase, pid)
                    p.used_bytes, p.status = int(p.quota_bytes or 0), models.UserStatus.quota_exceeded
                    user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller")
                    db.commit()
            check(L + "the consumption's reconciliation ran after its commit, the writer free", probes, ["free"])
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-void-hold-") as directory:
    engine = fixture.sqlite_engine(os.path.join(directory, "t.db"))
    try:
        scenario("sqlite", engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario("mariadb", engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
