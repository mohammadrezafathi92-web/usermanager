"""Two record-payment requests for the SAME approval at the SAME moment
count the payment once - with two real sessions on two threads, on SQLite
and on a real MariaDB.

Run:  python3 backend/tests/test_card_payment_concurrent.py

The interleaving that matters (Codex review of PR #29): request B has
already started its transaction and read things - on MariaDB that fixes
its REPEATABLE READ snapshot - when request A commits its event; B then
gets the pool lock. If B decides "no event yet" from its old snapshot it
counts the payment a second time, hits the unique approval_uuid, and (with
the old code) switched the pool's event log off.

The test forces exactly that order:
  A takes the pool lock, writes its event, and does not commit until B has
  read and is about to ask for the pool lock; then A commits and B runs on.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, event, inspect as sa_inspect, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, payment_card_events as events, payment_cards, receipt_void_schema
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg

failures: list[str] = []
PE = rv.payment_card_pool_events
AMOUNT = 5000


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def drop_everything(engine):
    with engine.begin() as conn:
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")
        for name in sa_inspect(conn).get_table_names():
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{name}`")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def scenario(label: str, engine) -> None:
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check(f"{label}: schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)
    internal = bot_auth.BotPrincipal.internal(None)
    db = Session()
    db.add_all([models.PanelSettings(id=1), models.User(username="ali", telegram_id=111),
                models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)])
    db.commit()
    card = payment_cards.create_card(db, None, {"card_number": "6037-1", "card_holder": "H"})   # new pool: event_logged
    db.commit()
    card_id = card.id
    approval = reg.register(db, internal, ri.ApprovalIntent(
        pending_source_instance_id="inst", pending_local_id=1, kind="topup", target_username="ali", amount=AMOUNT,
        payment_card_id=card_id), approval_mode="manual", approved_by_telegram_id=1000)
    db.commit()
    uuid_ = approval.approval_uuid
    db.close()

    real_phase, real_record = events.phase_for_payment, events.record_payment

    def run_pair(approval_uuid):
        """Requests A and B at the same moment, forced into the order
        described at the top of this file. Returns {name: answer}."""
        a_has_lock, b_is_waiting = threading.Event(), threading.Event()
        results: dict[str, object] = {}
        started: set[str] = set()

        def phase_hook(db_, owner):
            name = threading.current_thread().name
            first_time = name not in started
            started.add(name)
            if name == "B":
                if first_time:
                    b_is_waiting.set()       # B has read (its snapshot exists) and now asks for the lock
                return real_phase(db_, owner)
            phase = real_phase(db_, owner)
            a_has_lock.set()
            return phase

        def record_hook(*args, **kwargs):
            written = real_record(*args, **kwargs)
            if threading.current_thread().name == "A":
                b_is_waiting.wait(3)         # hold the lock, uncommitted, until B is queued behind it
                time.sleep(0.4)
            return written

        def request(name):
            session = Session()
            try:
                if name == "B":
                    a_has_lock.wait(5)
                results[name] = bot_router.record_payment_card_use(
                    card_id, schemas.BotRecordCardPaymentRequest(amount=AMOUNT, approval_uuid=approval_uuid),
                    db=session, principal=internal)
            except Exception as exc:  # noqa: BLE001
                results[name] = f"{type(exc).__name__}: {exc}"
            finally:
                session.close()

        events.phase_for_payment, events.record_payment = phase_hook, record_hook
        try:
            threads = [threading.Thread(target=request, args=(name,), name=name) for name in ("A", "B")]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        finally:
            events.phase_for_payment, events.record_payment = real_phase, real_record
        return results

    results = run_pair(uuid_)

    db = Session()
    rows = [(r.event_kind, r.approval_uuid == uuid_, int(r.amount)) for r in db.execute(select(PE).order_by(PE.c.id))]
    counter = int(db.get(models.PaymentCard, card_id).accumulated_amount or 0)
    check(f"{label}: both requests answered ok", results, {"A": {"ok": True}, "B": {"ok": True}})
    check(f"{label}: exactly ONE event for the approval, and the counter counted it once",
          (rows, counter), ([("payment_recorded", True, AMOUNT)], AMOUNT))
    check(f"{label}: the pool's event log was NOT switched off, and still equals the counter",
          (events.lock_pool(db, None, create_as=None), events.accumulator(db, None, card_id)), ("event_logged", AMOUNT))
    db.rollback()
    third = bot_router.record_payment_card_use(card_id, schemas.BotRecordCardPaymentRequest(amount=AMOUNT, approval_uuid=uuid_),
                                               db=db, principal=internal)
    check(f"{label}: a later repeat changes nothing either",
          (third, int(db.get(models.PaymentCard, card_id).accumulated_amount or 0)), ({"ok": True}, AMOUNT))
    db.close()

    # Two DIFFERENT payments on the same card at the same moment (no
    # approval): both must be counted, each with its own event - the one
    # that waited must not fail on a stale view of the card row.
    results = run_pair(None)
    db = Session()
    kinds = [r.event_kind for r in db.execute(select(PE).order_by(PE.c.id))]
    counter = int(db.get(models.PaymentCard, card_id).accumulated_amount or 0)
    check(f"{label}: two different simultaneous payments are BOTH counted, with one event each",
          (results, counter, kinds, events.accumulator(db, None, card_id), events.lock_pool(db, None, create_as=None)),
          ({"A": {"ok": True}, "B": {"ok": True}}, 3 * AMOUNT,
           ["payment_recorded", "payment_recorded_uncorrelated", "payment_recorded_uncorrelated"], 3 * AMOUNT, "event_logged"))
    db.rollback()
    db.close()


print("--- SQLite (WAL, busy timeout - the application's own engine settings) ---")
sqlite_engine = create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db", connect_args={"check_same_thread": False})


@event.listens_for(sqlite_engine, "connect")
def _pragmas(dbapi_connection, _record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=15000")
    cursor.close()


scenario("SQLite", sqlite_engine)

print("--- real MariaDB (REPEATABLE READ) ---")
mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    maria = create_engine(mariadb_url)
    drop_everything(maria)
    try:
        with maria.connect() as conn:
            isolation = conn.exec_driver_sql("SELECT @@transaction_isolation").scalar()
        check("MariaDB runs at REPEATABLE READ, the level the review is about", isolation, "REPEATABLE-READ")
        scenario("MariaDB", maria)
    finally:
        drop_everything(maria)
        maria.dispose()
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
