"""A2 ACCEPTANCE REGRESSION: what does the wallet top-up endpoint do when
a receipt approval's uuid is used again - with the same request, at the
same moment, or with a request that does not match the approval at all?

Run:  python3 backend/tests/repro_topup_idempotency.py

The green-suite wrapper `test_receipt_approval_topup_idempotency.py` runs
this same harness as a permanent acceptance regression.

INVARIANT. For ONE receipt approval (one approval_uuid):
  I1  however often `POST /api/bot/users/{username}/add-balance` is sent
      for it - in a row, or two at the same moment - the wallet grows by
      the amount exactly once, exactly one `wallet_topup` ledger row exists,
      and balance and ledger agree;
  I2  the repeat answers successfully and changes nothing;
  I3  a mismatched shadow request is treated as a separate uncorrelated
      legacy credit and logged; it never overwrites the approval's original
      correlation;
  I4  an unknown approval UUID in shadow is likewise uncorrelated and logged.
      Required/blocked mode fails closed instead.

HOW IT IS DRIVEN. Real HTTP requests (FastAPI TestClient) against the real
routers - routers/bot.py and the receipt-approval bot router - with the
real X-API-Key dependency and a fresh database session per request, on a
temporary database with made-up data.
  - SQLite: a file in a TemporaryDirectory, WAL + busy timeout like the
    application's engine.
  - MariaDB: only through tests/_mariadb_scratch.py, which creates and
    drops its own run database. Never an existing database.
Three kinds of run, kept apart and labelled:
  SEQUENTIAL             one request after the other. Deterministic. This is
                         a retry after a lost HTTP response.
  CONCURRENT-FREE        two real threads released together by a barrier;
                         nothing forces how they interleave, so it only
                         shows that the invariant is violated, not how.
  CONCURRENT-LOCK-BOUNDARY two real threads are held at entry to the
                         transaction-start helper, then released together.
                         This overlaps requests but deliberately does not
                         bypass the serialization lock to force stale reads.

No node is involved: no customer here has a connection, so no endpoint
builds a config. When tests/_no_network.py is present (added by an open
pull request) it is installed as proof.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

import _mariadb_scratch as scratch

try:                                        # added by an open pull request; used when it is there
    import _no_network
    _no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
except ImportError:
    _no_network = None

from app import models, models_receipt_void as rv
from app.database import get_db
from app.routers import bot as bot_router
from app.routers import receipt_approvals as approvals_router
from app.services import accounting, bot_auth, payment_cards, receipt_void_schema
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []
AMOUNT = 50_000
A, F, SH, PE = (rv.receipt_approvals, rv.receipt_approval_effects, rv.receipt_approval_shadow_events,
                rv.payment_card_pool_events)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label: str, engine) -> None:
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

    setup = Session()
    key = models.ApiKey(key=f"repro-key-{label}", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    names = ("buyer_seq", "buyer_fail", "buyer_free", "buyer_ctrl", "buyer_m", "buyer_other", "buyer_unknown",
             "buyer_plain", "buyer_effect")
    setup.add_all([models.PanelSettings(id=1), key,
                   models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)]
                  + [models.User(username=name, telegram_id=100 + i, balance=0) for i, name in enumerate(names)])
    setup.commit()
    card = payment_cards.create_card(setup, None, {"card_number": "6037-0001", "card_holder": "H"})   # new pool: event_logged
    card_two = payment_cards.create_card(setup, None, {"card_number": "6037-0002", "card_holder": "H", "sort_order": 1})
    runtime.set_requested_mode(setup, registration_mode="shadow")
    setup.commit()
    card_id, other_card_id, headers = card.id, card_two.id, {"X-API-Key": key.key}
    telegram = {name: 100 + i for i, name in enumerate(names)}
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

    def money(username):
        """One customer's wallet and ledger, read back from the database."""
        session = Session()
        try:
            user = session.query(models.User).filter_by(username=username).one()
            ledger = session.query(models.LedgerEntry).filter_by(user_id=user.id, kind="wallet_topup").order_by(models.LedgerEntry.id).all()
            return {"balance": int(user.balance or 0), "ledger_rows": len(ledger),
                    "ledger_total": sum(int(r.amount or 0) for r in ledger),
                    "ledger": [(int(r.amount), r.payment_card_id, r.approval_uuid) for r in ledger]}
        finally:
            session.close()

    def approval(approval_uuid):
        """One approval's own records, read back from the database."""
        session = Session()
        try:
            return {
                "state": session.execute(select(A.c.state).where(A.c.approval_uuid == approval_uuid)).scalar(),
                "effects": sorted(f"{r.effect_type}:{r.effect_key}" for r in session.execute(
                    select(F).where(F.c.approval_uuid == approval_uuid))),
                "effect_resources": sorted((r.effect_key, r.resource_type, r.resource_id, r.delta_value) for r in session.execute(
                    select(F).where(F.c.approval_uuid == approval_uuid))),
                "ledger_rows_tagged": session.query(models.LedgerEntry).filter_by(approval_uuid=approval_uuid).count(),
                "shadow_errors": sorted(r.error_code for r in session.execute(
                    select(SH).where(SH.c.approval_uuid == approval_uuid))),
            }
        finally:
            session.close()

    def untied_shadow_errors():
        session = Session()
        try:
            return sorted(r.error_code for r in session.execute(select(SH).where(SH.c.approval_uuid.is_(None))))
        finally:
            session.close()

    pending = [0]

    def register(client, username, amount=AMOUNT, payment_card_id=card_id):
        pending[0] += 1
        response = client.post("/api/accounting/receipt-approvals/manual", headers=headers, json={
            "pending_source_instance_id": f"repro-{label}", "pending_local_id": pending[0], "kind": "topup",
            "target_username": username, "amount": amount, "payment_card_id": payment_card_id,
            "telegram_id": telegram[username], "approved_by_telegram_id": 1000})
        body = response.json()
        return response.status_code, body.get("approval_uuid")

    def register_again(client, username, local_id, amount=AMOUNT, payment_card_id=card_id):
        response = client.post("/api/accounting/receipt-approvals/manual", headers=headers, json={
            "pending_source_instance_id": f"repro-{label}", "pending_local_id": local_id, "kind": "topup",
            "target_username": username, "amount": amount, "payment_card_id": payment_card_id,
            "telegram_id": telegram[username], "approved_by_telegram_id": 1000})
        return response.status_code, response.json()

    def top_up(client, username, approval_uuid, amount=AMOUNT, payment_card_id=card_id):
        response = client.post(f"/api/bot/users/{username}/add-balance", headers=headers,
                               json={"amount": amount, "payment_card_id": payment_card_id, "approval_uuid": approval_uuid})
        is_json = response.headers.get("content-type", "").startswith("application/json")
        return response.status_code, (response.json().get("balance") if is_json else None)

    client = TestClient(app)

    # ------------------------------------------------------------------ SEQUENTIAL
    print(f"--- {label}: SEQUENTIAL - the same request again (a retry after a lost response) ---")
    status, uuid_seq = register(client, "buyer_seq")
    check(L + "approval registered", (status, bool(uuid_seq)), (201, True))
    first = top_up(client, "buyer_seq", uuid_seq)
    after_first, approval_first = money("buyer_seq"), approval(uuid_seq)
    check(L + "first call: 200, +amount, one ledger row tagged with the approval, two effects",
          (first, after_first["balance"], after_first["ledger"], approval_first["effects"]),
          ((200, AMOUNT), AMOUNT, [(AMOUNT, card_id, uuid_seq)],
           ["ledger_topup:topup", "wallet_credit_source_created:credit:receipt_topup"]))
    second = top_up(client, "buyer_seq", uuid_seq)
    after_second, approval_second = money("buyer_seq"), approval(uuid_seq)
    print(f"      observed: repeat -> HTTP {second[0]} balance {second[1]} ; {after_second} ; {approval_second}")
    check(L + "I2 the repeat answers 200 with the balance unchanged", second, (200, AMOUNT))
    check(L + "I1 the wallet is credited exactly once", after_second["balance"], AMOUNT)
    check(L + "I1 exactly one wallet_topup ledger row", (after_second["ledger_rows"], after_second["ledger_total"]), (1, AMOUNT))
    check(L + "the approval's effect rows do not change on the repeat (they are keyed by approval)",
          approval_second["effect_resources"], approval_first["effect_resources"])

    # Legacy shadow code could commit a ledger row, then fail to record
    # effects and mark the approval failed. Prove all three recovery entry
    # points treat the tagged ledger as mutation evidence.
    print(f"--- {label}: failed approval plus tagged ledger is recovered, never credited twice or failed again ---")
    session = Session()
    version_before = session.execute(select(A.c.version).where(A.c.approval_uuid == uuid_seq)).scalar_one()
    session.execute(F.delete().where(F.c.approval_uuid == uuid_seq))  # emulate the old refused-effect write
    session.execute(A.update().where(A.c.approval_uuid == uuid_seq).values(
        state="failed", failed_at=dt.datetime.utcnow()))
    session.commit()
    session.close()
    status_re, recovered = register_again(client, "buyer_seq", 1)
    recovered_row = approval(uuid_seq)
    check(L + "re-register recovers failed -> mutating with the same version and no second approval",
          (status_re, recovered.get("approval_uuid"), recovered.get("state"), recovered.get("execution_token"),
           recovered_row["state"], len(recovered_row["shadow_errors"])),
          (200, uuid_seq, "mutating", version_before, "mutating", 1))
    no_credit = top_up(client, "buyer_seq", uuid_seq)
    check(L + "the add-balance recovery path returns success without another credit",
          (no_credit, money("buyer_seq")["balance"], money("buyer_seq")["ledger_rows"]),
          ((200, AMOUNT), AMOUNT, 1))
    finalize_failed = client.post(f"/api/accounting/receipt-approvals/{uuid_seq}/finalize",
                                  headers=headers, json={"failed": True})
    finalize_ok = client.post(f"/api/accounting/receipt-approvals/{uuid_seq}/finalize",
                              headers=headers, json={"failed": False})
    recovered_row = approval(uuid_seq)
    check(L + "both finalize outcomes remain mutating with one missing-effects audit and no second recovery event",
          (finalize_failed.status_code, finalize_failed.json().get("state"), finalize_ok.status_code,
           finalize_ok.json().get("state"), recovered_row["state"], recovered_row["shadow_errors"].count("missing_effects"),
           recovered_row["shadow_errors"].count("failed_with_ledger_recovered")),
          (200, "mutating", 200, "mutating", "mutating", 1, 1))

    print(f"--- {label}: effect evidence without a ledger tag is still never credited twice ---")
    _status, uuid_effect = register(client, "buyer_effect")
    effect_local_id = pending[0]
    first_effect_credit = top_up(client, "buyer_effect", uuid_effect)
    effect_money_before = money("buyer_effect")
    session = Session()
    effect_version_before = session.execute(
        select(A.c.version).where(A.c.approval_uuid == uuid_effect)).scalar_one()
    session.query(models.LedgerEntry).filter_by(approval_uuid=uuid_effect).update(
        {models.LedgerEntry.approval_uuid: None}, synchronize_session=False)
    session.execute(A.update().where(A.c.approval_uuid == uuid_effect).values(
        state="failed", failed_at=dt.datetime.utcnow()))
    session.commit()
    session.close()
    effect_re_status, effect_recovered = register_again(client, "buyer_effect", effect_local_id)
    effect_row = approval(uuid_effect)
    retry_effect_credit = top_up(client, "buyer_effect", uuid_effect)
    effect_money_after = money("buyer_effect")
    session = Session()
    effect_version_after = session.execute(
        select(A.c.version).where(A.c.approval_uuid == uuid_effect)).scalar_one()
    session.close()
    check(L + "effect-only evidence recovers failed -> mutating without changing the approval version",
          (effect_re_status, effect_recovered.get("state"), effect_row["state"],
           effect_version_after, effect_row["shadow_errors"].count("failed_with_effect_recovered")),
          (200, "mutating", "mutating", effect_version_before, 1))
    check(L + "the tagged-ledger loss is not mistaken for permission to credit again",
          (first_effect_credit, retry_effect_credit, effect_money_before["balance"], effect_money_after["balance"],
           effect_money_after["ledger_rows"], effect_money_after["ledger"][0][2]),
          ((200, AMOUNT), (200, AMOUNT), AMOUNT, AMOUNT, 1, None))

    # ------------------------------------------------------------------ one durable commit
    print(f"--- {label}: a ledger-write failure rolls back the credit and leaves the approval retryable ---")
    _status, uuid_fail = register(client, "buyer_fail")
    real_record = accounting.record

    def fail_ledger(*_args, **_kwargs):
        raise RuntimeError("injected ledger failure")

    accounting.record = fail_ledger
    failed_request = False
    try:
        try:
            top_up(client, "buyer_fail", uuid_fail)
        except RuntimeError as exc:
            failed_request = str(exc) == "injected ledger failure"
    finally:
        accounting.record = real_record
    after_rollback, approval_rollback = money("buyer_fail"), approval(uuid_fail)
    check(L + "the injected ledger failure propagated", failed_request, True)
    check(L + "no partial credit or ledger row survived; approval stayed retryable",
          (after_rollback["balance"], after_rollback["ledger_rows"], approval_rollback["state"],
           approval_rollback["effects"]), (0, 0, "registered", []))
    retried = top_up(client, "buyer_fail", uuid_fail)
    after_retry = money("buyer_fail")
    check(L + "retry after rollback credits once and records one ledger row",
          (retried[0], after_retry["balance"], after_retry["ledger_rows"]), (200, AMOUNT, 1))

    # ------------------------------------------------------------------ CONCURRENT, nothing forced
    print(f"--- {label}: CONCURRENT-FREE - two threads released together, interleaving NOT controlled ---")
    status, uuid_free = register(client, "buyer_free")
    barrier, results = threading.Barrier(2), {}

    def free_worker(name):
        own_client = TestClient(app)
        try:
            barrier.wait(10)
            results[name] = top_up(own_client, "buyer_free", uuid_free)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            results[name] = f"{type(exc).__name__}: {str(exc)[:120]}"

    threads = [threading.Thread(target=free_worker, args=(name,), name=name) for name in ("A", "B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    after_free = money("buyer_free")
    print(f"      observed: responses {results} ; {after_free}")
    check(L + "I1 (free) the wallet is credited exactly once", after_free["balance"], AMOUNT)
    check(L + "I1 (free) exactly one wallet_topup ledger row", (after_free["ledger_rows"], after_free["ledger_total"]), (1, AMOUNT))

    # ------------------------------------------------------------------ CONCURRENT, interleaving forced
    print(f"--- {label}: CONCURRENT-LOCK-BOUNDARY - requests reach transaction start before either locks ---")
    status, uuid_ctrl = register(client, "buyer_ctrl")
    both_at_boundary, results = threading.Barrier(2), {}
    from app.services import receipt_approval_topup
    real_start = receipt_approval_topup._start

    def synchronized_start(db):
        # Both real requests enter the transaction boundary before either
        # SQLite BEGIN IMMEDIATE / MariaDB row locks are acquired.
        if threading.current_thread().name in ("C1", "C2"):
            both_at_boundary.wait(15)
        return real_start(db)

    def controlled_worker(name):
        own_client = TestClient(app)
        try:
            results[name] = top_up(own_client, "buyer_ctrl", uuid_ctrl)
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {str(exc)[:120]}"

    receipt_approval_topup._start = synchronized_start
    try:
        threads = [threading.Thread(target=controlled_worker, args=(name,), name=name) for name in ("C1", "C2")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(90)
    finally:
        receipt_approval_topup._start = real_start
    after_ctrl, approval_ctrl = money("buyer_ctrl"), approval(uuid_ctrl)
    print(f"      observed: responses {results} ; {after_ctrl} ; effects {approval_ctrl['effects']} ; "
          f"shadow {approval_ctrl['shadow_errors']}")
    check(L + "I1 (controlled) the wallet is credited exactly once", after_ctrl["balance"], AMOUNT)
    check(L + "I1 (controlled) exactly one wallet_topup ledger row", (after_ctrl["ledger_rows"], after_ctrl["ledger_total"]), (1, AMOUNT))
    check(L + "I1 (controlled) balance and ledger agree with each other", after_ctrl["balance"], after_ctrl["ledger_total"])

    # ------------------------------------------------------------------ mismatched reuse
    print(f"--- {label}: the approval's uuid reused for a request it was NOT registered for ---")
    status, uuid_m = register(client, "buyer_m")
    top_up(client, "buyer_m", uuid_m)                                   # the legitimate top-up
    base_m, base_other, base_approval = money("buyer_m"), money("buyer_other"), approval(uuid_m)

    other_amount = top_up(client, "buyer_m", uuid_m, amount=70_000)
    now_m, now_approval = money("buyer_m"), approval(uuid_m)
    print(f"      different amount : HTTP {other_amount} ; {now_m} ; shadow {now_approval['shadow_errors']}")
    check(L + "S2 another amount is processed as an uncorrelated legacy top-up",
          (other_amount[0], now_m["balance"], now_m["ledger_rows"], now_m["ledger"][-1][2]),
          (200, base_m["balance"] + 70_000, base_m["ledger_rows"] + 1, None))
    base_m = money("buyer_m")

    other_user = top_up(client, "buyer_other", uuid_m)
    now_other, now_approval = money("buyer_other"), approval(uuid_m)
    print(f"      different user   : HTTP {other_user} ; other customer {now_other} ; "
          f"ledger rows tagged with this approval {now_approval['ledger_rows_tagged']} ; shadow {now_approval['shadow_errors']}")
    check(L + "S2 another customer is an uncorrelated legacy top-up, not tagged to this approval",
          (other_user[0], now_other["balance"], now_other["ledger_rows"], now_other["ledger"][0][2],
           now_approval["ledger_rows_tagged"]), (200, AMOUNT, 1, None, 1))

    other_card = top_up(client, "buyer_m", uuid_m, payment_card_id=other_card_id)
    now_m, now_approval = money("buyer_m"), approval(uuid_m)
    print(f"      different card   : HTTP {other_card} ; {now_m} ; shadow {now_approval['shadow_errors']}")
    check(L + "same customer+amount retry with another card is not credited twice",
          (other_card[0], now_m["balance"], now_m["ledger_rows"], now_m["ledger"][-1][2]),
          (200, base_m["balance"], base_m["ledger_rows"], None))
    check(L + "through all three mismatched requests the approval's own effect rows stayed exactly as first written",
          now_approval["effect_resources"], base_approval["effect_resources"])
    print(f"      the approval after all of it: state {now_approval['state']}, effects {now_approval['effects']}, "
          f"ledger rows carrying its uuid {now_approval['ledger_rows_tagged']}, shadow errors {now_approval['shadow_errors']}")
    check(L + "the mismatches left a trace somewhere (a shadow error) - otherwise nobody can ever tell they happened",
          bool(now_approval["shadow_errors"]), True)

    print(f"--- {label}: an approval_uuid the panel never issued ---")
    unknown_uuid = str(uuid.uuid4())
    shadow_before = untied_shadow_errors()
    unknown = top_up(client, "buyer_unknown", unknown_uuid)
    now_unknown = money("buyer_unknown")
    new_shadow = untied_shadow_errors()[len(shadow_before):] if len(untied_shadow_errors()) >= len(shadow_before) else []
    print(f"      unknown uuid     : HTTP {unknown} ; {now_unknown} ; new shadow errors not tied to an approval {new_shadow}")
    check(L + "S2 unknown UUID is a plain uncorrelated top-up",
          (unknown[0], now_unknown["balance"], now_unknown["ledger_rows"], now_unknown["ledger"][0][2],
           new_shadow), (200, AMOUNT, 1, None, ["approval_unknown"]))

    # ------------------------------------------------------------------ fail-closed when required is requested but not safe
    print(f"--- {label}: a requested required mode with missing token support blocks before credit ---")
    before_blocked = money("buyer_plain")
    guard = Session()
    guard.execute(rv.receipt_approval_runtime_state.update().where(
        rv.receipt_approval_runtime_state.c.id == 1).values(
            registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1))
    guard.commit()
    guard.close()
    blocked = client.post("/api/bot/users/buyer_plain/add-balance", headers=headers, json={
        "amount": AMOUNT, "payment_card_id": card_id, "approval_uuid": str(uuid.uuid4())})
    after_blocked = money("buyer_plain")
    check(L + "required is blocked until execution-token support exists",
          (blocked.status_code, blocked.json().get("detail")), (503, "receipt_approval_blocked"))
    check(L + "the blocked approval top-up changed no wallet or ledger data",
          (after_blocked["balance"], after_blocked["ledger_rows"]),
          (before_blocked["balance"], before_blocked["ledger_rows"]))
    blocked_missing_user_topup = client.post(
        "/api/bot/users/blocked_missing_user/add-balance", headers=headers,
        json={"amount": AMOUNT, "payment_card_id": card_id, "approval_uuid": uuid_seq})
    check(L + "blocked mode is returned before looking up the top-up target",
          (blocked_missing_user_topup.status_code, blocked_missing_user_topup.json().get("detail")),
          (503, "receipt_approval_blocked"))
    user_session = Session()
    before_users = user_session.query(models.User).count()
    user_session.close()
    blocked_create = client.post("/api/bot/users", headers=headers, json={
        "username": "blocked_should_not_exist", "telegram_id": 900_001,
        "approval_uuid": uuid_seq, "connections": []})
    user_session = Session()
    blocked_user_count = user_session.query(models.User).filter_by(
        username="blocked_should_not_exist").count()
    after_users = user_session.query(models.User).count()
    user_session.close()
    check(L + "a direct create-user call with a shadow approval is blocked before creating the user",
          (blocked_create.status_code, blocked_user_count, after_users),
          (503, 0, before_users))
    blocked_uuid = uuid_seq
    for path, payload in (
        ("/api/bot/users/blocked_missing_user/purchase-package",
         {"package_id": 999_991, "approval_uuid": blocked_uuid}),
        ("/api/bot/users/blocked_missing_user/renew",
         {"approval_uuid": blocked_uuid}),
        ("/api/bot/users/blocked_missing_user/purchases/999_992/renew",
         {"approval_uuid": blocked_uuid}),
        ("/api/bot/discount/redeem",
         {"code": "BLOCKED", "username": "blocked_missing_user", "approval_uuid": blocked_uuid}),
        ("/api/bot/users/blocked_missing_user/add-balance",
         {"amount": -1, "approval_uuid": blocked_uuid}),
        ("/api/bot/users/blocked_missing_user/add-balance",
         {"amount": 0, "approval_uuid": blocked_uuid}),
    ):
        response = client.post(path, headers=headers, json=payload)
        check(L + f"blocked mode wins before endpoint lookups for {path} amount={payload.get('amount')}",
              (response.status_code, response.json().get("detail")), (503, "receipt_approval_blocked"))
    card_session = Session()
    card_before = card_session.query(models.PaymentCard).filter_by(id=card_id).one().accumulated_amount
    card_session.close()
    blocked_card = client.post("/api/bot/payment-cards/999_993/record-payment", headers=headers,
                               json={"amount": AMOUNT, "approval_uuid": uuid_seq})
    card_session = Session()
    card_after = card_session.query(models.PaymentCard).filter_by(id=card_id).one().accumulated_amount
    event_count_after = card_session.execute(select(PE.c.id).where(PE.c.approval_uuid == uuid_seq)).all()
    card_session.close()
    check(L + "blocked mode wins before the payment-card lookup and pool mutation",
          (blocked_card.status_code, card_after, len(event_count_after)), (503, card_before, 0))
    guard = Session()
    guard.execute(rv.receipt_approval_runtime_state.update().where(
        rv.receipt_approval_runtime_state.c.id == 1).values(
            registration_mode="shadow", loyalty_timing_mode="legacy_activation", completeness_generation=0))
    guard.commit()
    guard.close()

    # ------------------------------------------------------------------ control
    print(f"--- {label}: control - top-ups with NO approval are separate top-ups ---")
    plain = [client.post("/api/bot/users/buyer_plain/add-balance", headers=headers, json={"amount": AMOUNT}).status_code
             for _ in range(2)]
    plain_money = money("buyer_plain")
    check(L + "two top-ups without an approval are both credited (nothing identifies them as one)",
          (plain, plain_money["balance"], plain_money["ledger_rows"]), ([200, 200], 2 * AMOUNT, 2))
    if _no_network is not None:
        check(L + "no attempt to reach a node or any other host", _no_network.attempts, [])


def main() -> int:
    with contextlib.ExitStack() as cleanup:                 # runs on success AND on failure
        print("--- SQLite (WAL, busy timeout - the application's own engine settings) ---")
        folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="repro_topup_"))
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
                maria = scratch.claim(mariadb_url)          # its OWN new database; never an existing one
            except scratch.ScratchRefused as refused:
                check(f"MARIADB_TEST_URL must allow creating a run database ({refused})", False)
            else:
                cleanup.callback(scratch.release, maria)    # drops that run database and disposes the engine
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
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
