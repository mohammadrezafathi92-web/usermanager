"""A lease that expires (by the DB clock) while a request holds it, on
SQLite AND real MariaDB (CI). Fake runner; no network.

Evidence for the one-off LeaseLost of test_durable_replay_identity (local
full run 2026-10-16 02:36-03:04): the host's power log shows 'Idle Sleep'
02:38:13 -> wake 03:00:19, ~22 minutes; the 300 s leases of the operation in
flight were expired by the DB clock when the frozen process resumed, and
results.record's revalidation refused the stale result - the fencing doing
its job. Reproduced here deterministically by expiring the leases during
the remote call (what a long freeze does):

  * the request is answered 409 operation_in_progress (no raw LeaseLost);
  * nothing of the stale result was recorded: the step stays remote_calling;
  * the recovery worker reads the remote first and completes it exactly
    once: one service, one connection, the approval completed once.
"""
import contextlib
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-lease-expiry-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

import datetime as dt
from sqlalchemy import func, select

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_durable as durable, provisioning_parent_execute as execute
from app.services import remote_action as action
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        seen = []

        def frozen_past_ttl(dto):
            seen.append(bool((dto.params or {}).get("recovery_read")))
            if dto.action_type is action.ActionType.WG_ENSURE_PRESENT and len(seen) == 1:
                with env.Session() as db:                   # the host slept longer than every TTL
                    db.execute(rv.resource_locks.update().where(rv.resource_locks.c.lease_owner.isnot(None))
                               .values(leased_until=dt.datetime(2000, 1, 1)))
                    db.commit()
            return fixture.runner(dto)

        with patch.object(execute.remote_runner, "run_action", side_effect=frozen_past_ttl):
            item = env.begin("new", "victim", 1000, package_id=env.ids.shared, claimed_telegram_id=7001)
            from app import schemas
            from app.routers import bot as bot_router
            answer = env.run(lambda db: bot_router.purchase_package("victim", schemas.BotPurchasePackageRequest(
                package_id=env.ids.shared, paid_amount=1000, payment_method="card",
                approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=1000),
                db=db, owner_admin_id=None, principal=env.internal))
        with env.Session() as db:
            operation = db.execute(select(mp.ProvisioningOperation).where(
                mp.ProvisioningOperation.approval_uuid == item["approval_uuid"])).scalar_one()
            step = db.execute(select(mp.ProvisioningStep).where(
                mp.ProvisioningStep.operation_id == operation.id)).scalar_one()
            state = (operation.state, step.state)
        check(L + "leases expired during the remote call: answered 409 operation_in_progress, not a raw LeaseLost",
              answer, "operation_in_progress")
        check(L + "...the stale result was not recorded: the step is still remote_calling", state,
              ("provisioning", "remote_calling"))
        with env.Session() as db:
            db.execute(mp.ProvisioningOperation.__table__.update().values(next_retry_at=None))
            db.execute(mp.ProvisioningStep.__table__.update().values(next_retry_at=None))
            db.commit()
        with patch.object(execute.remote_runner, "run_action", side_effect=lambda dto: (
                seen.append(bool((dto.params or {}).get("recovery_read"))), fixture.runner(dto))[1]):
            for _ in range(4):
                durable.recover_due(env.Session)
        with env.Session() as db:
            operation = db.get(mp.ProvisioningOperation, operation.id)
            user = db.query(models.User).filter_by(username="victim").one()
            services = db.query(models.Purchase).filter_by(user_id=user.id).count()
            connections = db.query(models.Connection).filter_by(user_id=user.id).count()
            sales = db.execute(select(func.count()).select_from(models.LedgerEntry.__table__).where(
                models.LedgerEntry.approval_uuid == item["approval_uuid"])).scalar_one()
        check(L + "...the recovery reads the remote first, then completes exactly once",
              (operation.state, seen[1] if len(seen) > 1 else None, services, connections, sales,
               env.approval(item["approval_uuid"])["state"]), ("completed", True, 1, 1, 1, "completed"))


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="lease_expiry_"))
    sqlite = fixture.sqlite_engine(os.path.join(folder, "t.db"))
    cleanup.callback(sqlite.dispose)
    scenario("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        scenario("mariadb", maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
