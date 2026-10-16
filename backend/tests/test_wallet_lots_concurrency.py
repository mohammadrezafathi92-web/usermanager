"""The lot generation (Receipt Void P9) on SQLite and real MariaDB (CI):
the cutover's backfill and reconciliation, credit / hold / capture /
release with their row locks, and two debits at once on one account that
together exceed it - exactly one wins, nothing is overdrawn, the cache
equals the lots. No network.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import accounting, provisioning_readiness, provisioning_schema, receipt_void_schema
from app.services import wallet_accounts, wallet_adjustments, wallet_cutover, wallet_lots, wallet_reconcile

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Session)["ready"],
                                provisioning_schema.bootstrap(engine, Session)["ready"]), (True, True))
    with Session() as db:
        db.add(models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000))
        legacy = models.User(username="old", telegram_id=7001, balance=900)          # pre-P5: no account yet
        db.add(legacy)
        wallet_accounts.create_user_with_wallet(db, username="new", telegram_id=7002, balance=0)
        db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="shadow"))
        for kind in mp.OPERATION_TYPES:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        old_id, root_id = legacy.id, db.query(models.AdminUser.id).scalar()
    with Session() as db, patch.object(provisioning_readiness, "p6_ready", return_value=(True, [])):
        result = wallet_cutover.cutover(db)
    check(L + "cutover: a pre-P5 customer gets its account and baseline lot", (result["accounts_created"],
          result["baseline_lots"]), (1, 1))

    def account(db):
        return wallet_lots.account_of_user(db, old_id)

    # ------------------------------------------------------------------ credit / hold / capture / release
    with Session() as db:
        wallet_adjustments.adjust(db, old_id, 600, actor=db.get(models.AdminUser, root_id), reason="r",
                                  token=uuid.uuid4())
    with Session() as db:
        if engine.dialect.name == "sqlite":
            from sqlalchemy import text
            db.execute(text("BEGIN IMMEDIATE"))
        epoch = wallet_lots.require_enforced(db)
        acct = account(db)
        wallet_lots.fence_identity(db, acct["customer_identity_id"])
        op, _ = wallet_lots.operation(db, "wallet_debit", f"{acct['id']}:h1", payload={"a": 1000},
                                      tenant_scope_key="shared", epoch=epoch)
        wallet_lots.hold(db, acct, amount=1000, hold_ref="00000000-0000-4000-8000-000000000001", operation_id=op)
        user = db.get(models.User, old_id)
        entry = accounting.record(db, "sale_new", 1000, user=user, payment_method="wallet")
        db.flush()
        wallet_lots.capture(db, "00000000-0000-4000-8000-000000000001", entry.id)
        db.commit()
        allocations = [int(r.allocated_amount) for r in db.execute(select(rv.wallet_allocations).order_by(
            rv.wallet_allocations.c.id))]
        check(L + "a hold FIFO over two lots (900 baseline, then 600), captured with its sale",
              (allocations, int(db.get(models.User, old_id).balance)), ([900, 100], 500))
    with Session() as db:
        epoch = wallet_lots.require_enforced(db)
        acct = account(db)
        op, _ = wallet_lots.operation(db, "wallet_debit", f"{acct['id']}:h2", payload={"a": 300},
                                      tenant_scope_key="shared", epoch=epoch)
        wallet_lots.hold(db, acct, amount=300, hold_ref="00000000-0000-4000-8000-000000000002", operation_id=op)
        db.commit()
    with Session() as db:
        wallet_lots.require_enforced(db)
        op, _ = wallet_lots.operation(db, "wallet_refund", "debit:2", payload={"r": 1}, tenant_scope_key="shared",
                                      epoch=wallet_lots.require_enforced(db))
        check(L + "release: the held debit's money is back", wallet_lots.release(
            db, "00000000-0000-4000-8000-000000000002", op), True)
        db.commit()
        check(L + "...the cache equals the lots", int(db.get(models.User, old_id).balance), 500)

    # ------------------------------------------------------------------ two debits at once
    barrier, results = threading.Barrier(2), {}

    def debit(name):
        with Session() as db:
            try:
                barrier.wait(15)
                results[name] = wallet_adjustments.adjust(db, old_id, -400, actor=db.get(models.AdminUser, root_id),
                                                          reason="race", token=uuid.uuid4())
            except HTTPException as exc:
                results[name] = exc.status_code
            except Exception as exc:  # noqa: BLE001 - a lock conflict is reported, never hidden
                results[name] = type(exc).__name__

    threads = [threading.Thread(target=debit, args=(name,)) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    with Session() as db:
        final = int(db.get(models.User, old_id).balance)
        lots_total = wallet_lots.available(db, account(db)["id"])
        report = wallet_reconcile.check(db)
    winners = [value for value in results.values() if value == 100]
    print(f"      observed: {results}")
    check(L + "two debits of 400 against 500 at once: exactly one wins, nothing overdrawn",
          (len(winners), final, lots_total), (1, 100, 100))
    check(L + "...the other is a clean 'insufficient' (400), not a lock error", sorted(results.values()), [100, 400])
    check(L + "...the reconciliation finds nothing wrong", report["problems"], [])


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wallet_lots_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False, "timeout": 15})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    with patch("app.database.SessionLocal", sessionmaker(bind=sqlite, autoflush=False)):
        scenario("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        with patch("app.database.SessionLocal", sessionmaker(bind=maria, autoflush=False)):
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
