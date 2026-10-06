"""Receipt Void P5: services/wallet_service.py, the legacy writer of the
customer wallet (User.balance), and the phase rule of design 9.1-9.3.

What must hold:
  - in phase 'normal' every former writer does exactly what it did: top-up,
    wallet debit, loyalty reward, both referral rewards, the panel's edit;
  - in any other phase ('fencing', 'enforced') every one of them is refused
    with 503 wallet_legacy_writer_unavailable and the balance, the ledger
    and the reward flags are untouched;
  - a phase change waits for a writer that is in flight, that writer's
    credit commits, and the next writer sees the new phase (9.3);
  - two debits that together exceed the balance: exactly one succeeds;
  - while the Receipt Void schema is not ready there is no phase to read and
    the legacy writer keeps working.

Runs on SQLite always and, through tests/_mariadb_scratch.py, on a real
MariaDB (mandatory in CI): the phase read is a shared row lock there and
has no SQLite equivalent.
"""
from __future__ import annotations

import contextlib
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import _mariadb_scratch as scratch
import _no_network

_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv, schemas
from app.database import get_db
from app.routers import bot as bot_router
from app.routers import users as users_router
from app.services import bot_auth, receipt_void_schema, user_ops, wallet_service

failures: list[str] = []
AMOUNT = 50_000


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refusal(call):
    """(status, detail) of the HTTPException `call` raises, or what it returned."""
    try:
        return ("returned", call())
    except HTTPException as refused:
        return (refused.status_code, refused.detail)


REFUSED = (503, "wallet_legacy_writer_unavailable")


def scenario(label: str, engine) -> None:
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

    names = ("topup", "debit", "loyal", "referrer", "invited", "edited", "race", "inflight", "noschema")
    setup = Session()
    key = models.ApiKey(key=f"wallet-key-{label}", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    setup.add_all([
        models.PanelSettings(id=1, loyalty_purchase_threshold=1, loyalty_reward_credit=700,
                             referral_referrer_reward_credit=3000, referral_new_user_reward_credit=1000),
        key, admin,
    ] + [models.User(username=name, telegram_id=400 + i, balance=0) for i, name in enumerate(names)])
    setup.commit()
    setup.query(models.User).filter_by(username="referrer").one().referral_code = "WCODE"
    setup.commit()
    headers, admin_id = {"X-API-Key": key.key}, admin.id
    setup.close()

    def session_per_request():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(bot_router.router)
    app.dependency_overrides[get_db] = session_per_request
    client = TestClient(app)

    def state():
        """Everything a wallet writer could change."""
        session = Session()
        try:
            return {"users": {u.username: (u.balance, bool(u.referral_reward_granted), u.referred_by_id,
                                            int(u.loyalty_rewards_given or 0))
                              for u in session.query(models.User).order_by(models.User.id)},
                    "ledger_rows": session.query(models.LedgerEntry).count()}
        finally:
            session.close()

    def balance(username):
        return state()["users"][username][0]

    def set_phase(phase):
        session = Session()
        session.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1).values(
            phase=phase, epoch=rv.wallet_runtime_state.c.epoch + 1))
        session.commit()
        session.close()

    def add_balance(username, amount, own=None):
        response = (own or client).post(f"/api/bot/users/{username}/add-balance", headers=headers, json={"amount": amount})
        return response.status_code, (response.json().get("detail") if response.status_code != 200 else None)

    def loyalty(username):
        session = Session()
        try:
            user = session.query(models.User).filter_by(username=username).one()
            user.purchase_count = (user.purchase_count or 0) + 1
            user_ops._maybe_grant_loyalty_reward(session, user)
            session.commit()
        finally:
            session.rollback()
            session.close()

    def referral(username):
        session = Session()
        try:
            user = session.query(models.User).filter_by(username=username).one()
            return user_ops.apply_referral_code(session, user, "WCODE")[0]
        finally:
            session.rollback()
            session.close()

    def panel_edit(username, **fields):
        session = Session()
        try:
            user = session.query(models.User).filter_by(username=username).one()
            users_router.update_user(user.id, schemas.UserUpdate(**fields), db=session,
                                     admin=session.get(models.AdminUser, admin_id), _perm=None)
        finally:
            session.rollback()
            session.close()

    # ------------------------------------------------------------------ phase 'normal': nothing changed
    print(f"--- {label}: phase normal - every former writer works as before ---")
    check(L + "top-up credits the wallet", (add_balance("topup", AMOUNT), balance("topup")), ((200, None), AMOUNT))
    check(L + "debit opening credit", add_balance("debit", AMOUNT)[0], 200)
    check(L + "debit takes from the wallet", (add_balance("debit", -20_000), balance("debit")), ((200, None), 30_000))
    check(L + "an overdraft is refused and changes nothing",
          (add_balance("debit", -30_001)[0], balance("debit")), (400, 30_000))
    check(L + "a debit of exactly the balance is allowed", (add_balance("debit", -30_000)[0], balance("debit")), (200, 0))
    loyalty("loyal")
    check(L + "loyalty reward is credited once per threshold crossing", state()["users"]["loyal"], (700, False, None, 1))
    check(L + "referral is applied", referral("invited"), True)
    after_referral = state()["users"]
    check(L + "referral credits both sides", (after_referral["referrer"][0], after_referral["invited"][:2]),
          (3000, (1000, True)))
    panel_edit("edited", balance=12_345)
    check(L + "the panel's edit sets the balance", balance("edited"), 12_345)

    # ------------------------------------------------------------------ any other phase: refused, untouched
    for phase in ("fencing", "enforced"):
        print(f"--- {label}: phase {phase} - the legacy writer is refused everywhere ---")
        set_phase(phase)
        before = state()
        check(L + f"{phase}: top-up refused", add_balance("topup", AMOUNT), REFUSED)
        check(L + f"{phase}: debit refused", add_balance("topup", -1), REFUSED)
        check(L + f"{phase}: loyalty reward refused", refusal(lambda: loyalty("loyal")), REFUSED)
        check(L + f"{phase}: referral refused", refusal(lambda: referral("edited")), REFUSED)
        check(L + f"{phase}: the panel's balance edit refused", refusal(lambda: panel_edit("edited", balance=1)), REFUSED)
        check(L + f"{phase}: nothing changed - no balance, ledger row, referral link or loyalty count", state(), before)
        check(L + f"{phase}: a panel save that does not change the balance is not a wallet write",
              refusal(lambda: panel_edit("edited", balance=12_345, comment="note")), ("returned", None))
        session = Session()
        check(L + f"{phase}: each service function refuses on its own",
              [refusal(lambda: wallet_service.credit_atomic(session, 1, 1, source_kind=wallet_service.UNSPECIFIED)),
               refusal(lambda: wallet_service.debit_atomic(session, 1, 1, source_kind=wallet_service.SALE)),
               refusal(lambda: wallet_service.begin_writer(session))], [REFUSED] * 3)
        session.rollback()
        session.close()
    set_phase("normal")
    check(L + "back in normal the writer works again", (add_balance("topup", 1)[0], balance("topup")), (200, AMOUNT + 1))

    # ------------------------------------------------------------------ 9.3: a phase change waits for the writer in flight
    print(f"--- {label}: a phase change waits for a writer in flight; the next writer sees the new phase ---")
    writer = Session()
    wallet_service.begin_writer(writer)                     # the first statement of a wallet transaction
    inflight_id = writer.query(models.User).filter_by(username="inflight").one().id
    wallet_service.credit_atomic(writer, inflight_id, AMOUNT, source_kind=wallet_service.UNSPECIFIED, phase_checked=True)
    changed, problems = threading.Event(), []

    def change_phase():
        try:
            set_phase("fencing")
            changed.set()
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            problems.append(f"{type(exc).__name__}: {str(exc)[:120]}")

    changer = threading.Thread(target=change_phase)
    changer.start()
    waited = not changed.wait(1.0)
    check(L + "the phase change does not get in while the writer's transaction is open", waited, True)
    writer.commit()
    writer.close()
    changer.join(30)
    check(L + "it goes through once the writer commits", (changed.is_set(), problems), (True, []))
    check(L + "the writer's credit was committed under the old phase", balance("inflight"), AMOUNT)
    check(L + "the next writer sees the new phase", add_balance("inflight", 1), REFUSED)
    set_phase("normal")

    # ------------------------------------------------------------------ SQLite: a reward written mid-transaction
    # The rewards read the phase in the middle of a larger transaction and do
    # not hold SQLite's writer, and sqlite3 opens no transaction for a SELECT:
    # the phase can change between that read and the flush. The flush reads
    # it again, as the writer, and refuses. (On MariaDB the first read is a
    # shared lock, so the change waits - the case above.)
    if label == "sqlite":
        stale = Session()
        loyal = stale.query(models.User).filter_by(username="loyal").one()
        wallet_service.adjust_in_session(stale, loyal, 5, source_kind=wallet_service.LOYALTY_REWARD)
        set_phase("fencing")                                # another connection commits a phase change
        outcome = refusal(stale.commit)
        stale.rollback()
        stale.close()
        check(L + "a reward read under the old phase cannot commit after the phase changed",
              (outcome, balance("loyal")), (REFUSED, 700))
        set_phase("normal")
        fresh = Session()
        loyal = fresh.query(models.User).filter_by(username="loyal").one()
        wallet_service.adjust_in_session(fresh, loyal, 5, source_kind=wallet_service.LOYALTY_REWARD)
        fresh.commit()
        fresh.close()
        check(L + "with the phase unchanged the same write commits", balance("loyal"), 705)

    # ------------------------------------------------------------------ two debits, one balance
    print(f"--- {label}: two debits that together exceed the balance ---")
    check(L + "race opening credit", add_balance("race", AMOUNT)[0], 200)
    barrier, results = threading.Barrier(2), {}

    def debit_worker(name):
        own = TestClient(app)
        try:
            barrier.wait(15)
            results[name] = add_balance("race", -AMOUNT, own)[0]
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=debit_worker, args=(name,), name=name) for name in ("D1", "D2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    print(f"      observed: {results} ; balance {balance('race')}")
    check(L + "exactly one of the two debits succeeds and the wallet is not overdrawn",
          (sum(1 for status in results.values() if status == 200), balance("race")), (1, 0))
    if label == "sqlite":                                   # one writer at a time: the loser is a clean refusal
        check(L + "the other one is refused as insufficient balance", sorted(results.values()), [200, 400])

    # ------------------------------------------------------------------ schema not ready: no phase to read
    print(f"--- {label}: Receipt Void schema not ready - the legacy writer keeps working ---")
    set_phase("fencing")
    real_ready = receipt_void_schema.is_ready
    receipt_void_schema.is_ready = lambda: False
    try:
        check(L + "top-up and debit work without reading a phase",
              (add_balance("noschema", AMOUNT)[0], add_balance("noschema", -1)[0], balance("noschema")),
              (200, 200, AMOUNT - 1))
    finally:
        receipt_void_schema.is_ready = real_ready
        set_phase("normal")

    check(L + "no attempt to reach a node or any other host", _no_network.attempts, [])


# ------------------------------------------------------------------ argument rules (no database needed)
for label, call in (
    ("a credit with a debit-only kind", lambda: wallet_service.credit_atomic(None, 1, 1, source_kind=wallet_service.SALE)),
    ("a debit with a credit-only kind", lambda: wallet_service.debit_atomic(None, 1, 1, source_kind=wallet_service.RECEIPT_TOPUP)),
    ("an unknown kind", lambda: wallet_service.credit_atomic(None, 1, 1, source_kind="gift")),
    ("a zero credit", lambda: wallet_service.credit_atomic(None, 1, 0, source_kind=wallet_service.UNSPECIFIED)),
    ("a negative debit", lambda: wallet_service.debit_atomic(None, 1, -5, source_kind=wallet_service.SALE)),
    ("an overwrite that is not a manual adjustment",
     lambda: wallet_service.overwrite(None, None, 1, source_kind=wallet_service.UNSPECIFIED)),
    ("a reward path used for a top-up",
     lambda: wallet_service.adjust_in_session(None, None, 1, source_kind=wallet_service.RECEIPT_TOPUP)),
):
    try:
        call()
        outcome = "accepted"
    except ValueError:
        outcome = "ValueError"
    check(f"refused before touching the database: {label}", outcome, "ValueError")

with contextlib.ExitStack() as cleanup:                     # runs on success AND on failure
    print("--- SQLite (WAL, busy timeout - the application's own engine settings) ---")
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="wallet_service_"))
    sqlite_engine = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite_engine.dispose)

    @event.listens_for(sqlite_engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    scenario("sqlite", sqlite_engine)

    print("--- real MariaDB ---")
    mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if mariadb_url:
        try:
            maria = scratch.claim(mariadb_url)              # its OWN new database; never an existing one
        except scratch.ScratchRefused as refused:
            check(f"MARIADB_TEST_URL must allow creating a run database ({refused})", False)
        else:
            cleanup.callback(scratch.release, maria)        # drops that run database and disposes the engine
            scenario("mariadb", maria)
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
