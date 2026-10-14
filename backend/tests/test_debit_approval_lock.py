"""P5a finding 1: an approval-carrying wallet debit keeps the approval-mode
lock (and on SQLite the writer) from its check until its commit.

A debit with an approval_uuid is paused between the mode check and the
debit; meanwhile another session switches the receipt-approval mode to
'required' (effectively 'blocked'). The switch must WAIT for the debit's
commit - it can never land between the check and the write. A debit that
starts after the switch is refused. SQLite always; real MariaDB in CI.
"""
import contextlib
import os
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, receipt_void_schema, wallet_accounts, wallet_service
from app.services import receipt_approval_runtime as runtime

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
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"])
    with Session() as db:
        db.add(models.PanelSettings(id=1))
        wallet_accounts.create_user_with_wallet(db, username="payer", telegram_id=5, balance=1000)
        db.commit()
        runtime.set_requested_mode(db, registration_mode="shadow")
        db.commit()
    principal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
    inside, release = threading.Event(), threading.Event()
    real_debit = wallet_service.debit_atomic

    def paused_debit(*args, **kwargs):
        inside.set()
        release.wait(15)
        return real_debit(*args, **kwargs)

    results, order = {}, []

    def debit():
        with Session() as db, patch.object(wallet_service, "debit_atomic", side_effect=paused_debit):
            # Recorded at the debit's COMMIT, while its locks are still held:
            # a switch that waited for them can only finish after this.
            event.listen(db, "before_commit", lambda _session: order.append("debit_committed"))
            try:
                bot_router.add_balance("payer", schemas.BotAddBalanceRequest(amount=-100, approval_uuid=str(uuid.uuid4())),
                                       db=db, principal=principal)
                results["debit"] = "ok"
            except HTTPException as exc:
                results["debit"] = exc.detail

    def switch():
        with Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().where(rv.receipt_approval_runtime_state.c.id == 1)
                       .values(registration_mode="required", key_identity_ready=False, key_identity_failure="duplicate_uuid", loyalty_timing_mode="payment_event",
                               completeness_generation=1))
            db.commit()
        order.append("mode_switched")

    worker = threading.Thread(target=debit)
    worker.start()
    check(L + "the debit reached its write after the mode check", inside.wait(15))
    switcher = threading.Thread(target=switch)
    switcher.start()
    time.sleep(1.0)
    waited = not order
    release.set()
    worker.join(30)
    switcher.join(30)
    check(L + "the mode switch waited while the checked debit was in flight", waited, True)
    check(L + "the debit committed first, then the switch", (results.get("debit"), order),
          ("ok", ["debit_committed", "mode_switched"]))
    with Session() as db:
        check(L + "the debit took exactly its amount", db.query(models.User).filter_by(username="payer").one().balance, 900)
        try:
            bot_router.add_balance("payer", schemas.BotAddBalanceRequest(amount=-100, approval_uuid=str(uuid.uuid4())),
                                   db=db, principal=principal)
            after = "ok"
        except HTTPException as exc:
            after = exc.detail
    check(L + "a debit that starts after the switch is refused", after, "receipt_approval_blocked")


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="debit_lock_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

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
