"""A2: the customer wallet (User.balance) and its ledger stay consistent
when DIFFERENT top-ups of one customer arrive at the same moment.

The acceptance harness (repro_topup_idempotency.py) covers one approval
used twice. This file covers what must NOT be collapsed or lost:
  - two different approvals of one customer, together -> both credited,
    each exactly once, each ledger row tagged with its own approval;
  - several top-ups without an approval, together -> none lost (the old
    read-modify-write lost one of two);
  - a credit and a debit together -> the balance is the exact sum;
  - only services/receipt_approval_topup.py writes a wallet_topup row.

Real HTTP requests against the real router, a fresh session per request.
SQLite (WAL file) always; real MariaDB only through _mariadb_scratch.
"""
from __future__ import annotations

import ast
import contextlib
import os
import pathlib
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import _mariadb_scratch as scratch

from app import models
from app.database import get_db
from app.routers import bot as bot_router
from app.routers import receipt_approvals as approvals_router
from app.services import bot_auth, payment_cards, receipt_void_schema
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []
AMOUNT = 50_000
PLAIN_REQUESTS = 6


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def together(app, calls):
    """Run each (name, function-of-client) on its own thread, released at once."""
    barrier, results = threading.Barrier(len(calls)), {}

    def worker(name, call):
        own_client = TestClient(app)
        try:
            barrier.wait(15)
            results[name] = call(own_client)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            results[name] = f"{type(exc).__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=worker, args=call, name=call[0]) for call in calls]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(90)
    return results


def scenario(label: str, engine) -> None:
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

    names = ("two_approvals", "plain_many", "credit_debit")
    setup = Session()
    key = models.ApiKey(key=f"conc-key-{label}", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    setup.add_all([models.PanelSettings(id=1), key,
                   models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)]
                  + [models.User(username=name, telegram_id=300 + i, balance=0) for i, name in enumerate(names)])
    setup.commit()
    card = payment_cards.create_card(setup, None, {"card_number": "6037-0009", "card_holder": "H"})
    runtime.set_requested_mode(setup, registration_mode="shadow")
    setup.commit()
    card_id, headers = card.id, {"X-API-Key": key.key}
    telegram = {name: 300 + i for i, name in enumerate(names)}
    setup.close()

    def session_per_request():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app = FastAPI()
    app.include_router(bot_router.router)
    app.include_router(approvals_router.bot_router)
    app.dependency_overrides[get_db] = session_per_request
    client = TestClient(app)

    def money(username):
        session = Session()
        try:
            user = session.query(models.User).filter_by(username=username).one()
            rows = session.query(models.LedgerEntry).filter_by(
                user_id=user.id, kind="wallet_topup").order_by(models.LedgerEntry.id).all()
            return {"balance": int(user.balance or 0),
                    "ledger_total": sum(int(r.amount or 0) for r in rows),
                    "rows": len(rows), "tags": sorted(r.approval_uuid or "" for r in rows)}
        finally:
            session.close()

    def register(username, local_id):
        response = client.post("/api/accounting/receipt-approvals/manual", headers=headers, json={
            "pending_source_instance_id": f"conc-{label}", "pending_local_id": local_id, "kind": "topup",
            "target_username": username, "amount": AMOUNT, "payment_card_id": card_id,
            "telegram_id": telegram[username], "approved_by_telegram_id": 1000})
        return response.json().get("approval_uuid")

    def add(username, amount, approval_uuid=None):
        body = {"amount": amount}
        if approval_uuid:
            body.update(payment_card_id=card_id, approval_uuid=approval_uuid)
        return lambda own: own.post(f"/api/bot/users/{username}/add-balance", headers=headers, json=body).status_code

    # ------------------------------------------------------------------ two different approvals, one customer
    first, second = register("two_approvals", 1), register("two_approvals", 2)
    check(L + "two separate approvals registered", bool(first and second and first != second), True)
    results = together(app, [("A", add("two_approvals", AMOUNT, first)), ("B", add("two_approvals", AMOUNT, second))])
    now = money("two_approvals")
    print(f"      observed: {results} ; {now}")
    check(L + "two different approvals together: both answer 200", sorted(results.values()), [200, 200])
    check(L + "two different approvals together: both credited, balance and ledger agree",
          (now["balance"], now["ledger_total"]), (2 * AMOUNT, 2 * AMOUNT))
    check(L + "each ledger row carries its own approval", now["tags"], sorted([first, second]))
    again = together(app, [("A", add("two_approvals", AMOUNT, first)), ("B", add("two_approvals", AMOUNT, second))])
    check(L + "repeating both together changes nothing",
          (sorted(again.values()), money("two_approvals")), ([200, 200], now))

    # ------------------------------------------------------------------ plain top-ups, none lost
    results = together(app, [(f"P{i}", add("plain_many", AMOUNT)) for i in range(PLAIN_REQUESTS)])
    now = money("plain_many")
    print(f"      observed: {results} ; {now}")
    credited = sum(1 for status in results.values() if status == 200)
    if label == "sqlite":                                   # one writer, 15 s busy timeout: nobody is turned away
        check(L + f"{PLAIN_REQUESTS} top-ups without an approval together: all answer 200", credited, PLAIN_REQUESTS)
    else:                                                   # a row conflict may exhaust the retries: 503, never a loss
        check(L + "every answer is 200 or a clean 503", sorted(set(results.values())) in ([200], [200, 503]), True)
        check(L + "at least one is credited", credited >= 1, True)
    check(L + "none of them is lost: balance and ledger agree with the 200 answers, one row each",
          (now["balance"], now["ledger_total"], now["rows"]), (credited * AMOUNT, credited * AMOUNT, credited))

    # ------------------------------------------------------------------ credit and debit together
    check(L + "opening credit", add("credit_debit", 3 * AMOUNT)(client), 200)
    results = together(app, [("C", add("credit_debit", AMOUNT)), ("D", add("credit_debit", -2 * AMOUNT))])
    now = money("credit_debit")
    print(f"      observed: {results} ; {now}")
    if label == "sqlite":
        check(L + "a credit and a debit together: both answer 200", sorted(results.values()), [200, 200])
    # (the debit path is older than A2 and has no retry of its own; on
    # MariaDB a row conflict may refuse it - what must hold is the sum)
    expected = 3 * AMOUNT + (AMOUNT if results["C"] == 200 else 0) - (2 * AMOUNT if results["D"] == 200 else 0)
    check(L + "the balance is the exact sum of what was answered 200", now["balance"], expected)
    check(L + "the debit wrote no wallet_topup row",
          now["ledger_total"], 3 * AMOUNT + (AMOUNT if results["C"] == 200 else 0))
    refused = add("credit_debit", -(expected + 1))(client)
    check(L + "an overdraft is still refused and changes nothing", (refused, money("credit_debit")["balance"]),
          (400, expected))


    # ------------------------------------------------------------------ refusals keep their log line
    # A refusal raises after writing its shadow event; the event must be
    # committed first or the retry wrapper's rollback erases it.
    from app import models_receipt_void as rv
    used = register("two_approvals", 3)
    check(L + "third approval credited", add("two_approvals", AMOUNT, used)(client), 200)
    session = Session()
    session.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == used).values(state="voided"))
    session.commit()
    session.close()
    before = money("two_approvals")
    refused = client.post("/api/bot/users/two_approvals/add-balance", headers=headers, json={
        "amount": AMOUNT + 1, "payment_card_id": card_id, "approval_uuid": used})
    session = Session()
    events = sorted(r.error_code for r in session.execute(select(rv.receipt_approval_shadow_events).where(
        rv.receipt_approval_shadow_events.c.approval_uuid == used)))
    session.close()
    check(L + "a voided approval that already credited refuses a different amount and credits nothing",
          (refused.status_code, refused.json().get("detail"), money("two_approvals")),
          (409, "approval_has_effects", before))
    check(L + "the refusal is kept in the shadow log", "topup_already_applied" in events, True)


def wallet_topup_writers() -> list[str]:
    """Every app module that calls record(..., "wallet_topup", ...)."""
    root = pathlib.Path(__file__).resolve().parent.parent / "app"
    found = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", getattr(node.func, "id", "")) != "record":
                continue
            values = list(node.args) + [keyword.value for keyword in node.keywords]
            if any(isinstance(v, ast.Constant) and v.value == "wallet_topup" for v in values):
                found.add(str(path.relative_to(root)))
    return sorted(found)


check("only receipt_approval_topup writes a wallet_topup ledger row",
      wallet_topup_writers(), ["services/receipt_approval_topup.py"])

with contextlib.ExitStack() as cleanup:                     # runs on success AND on failure
    print("--- SQLite (WAL, busy timeout - the application's own engine settings) ---")
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="topup_conc_"))
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
