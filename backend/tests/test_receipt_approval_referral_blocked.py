"""A referral applied for a receipt approval is refused while the effective
registration mode is blocked - before the customer is looked up, and
without writing a reward, a referral link, an effect or a shadow event.

`POST /api/bot/referral/apply` carries an approval_uuid when it is part of
a receipt approval, so it is an approval-carrying mutation like purchase,
renewal and top-up (design 5.5: in blocked every such mutation answers 503
receipt_approval_blocked and no legacy path runs). Without an approval the
referral is the old flow and is not gated.

Real HTTP requests against the real router. No node is involved.
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import _no_network

_no_network.install([])
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app import models, models_receipt_void as rv
from app.database import get_db
from app.routers import bot as bot_router
from app.services import bot_auth, receipt_void_schema
from app.services import receipt_approval_runtime as runtime

failures: list[str] = []
GB = 1024 ** 3


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


with tempfile.TemporaryDirectory(prefix="referral_blocked_") as folder:
    engine = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)        # the application's settings
    check("schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)

    setup = Session()
    key = models.ApiKey(key="referral-blocked-key", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    setup.add_all([
        models.PanelSettings(id=1, referral_referrer_reward_credit=3000, referral_referrer_reward_gb=2,
                             referral_new_user_reward_credit=1000, referral_new_user_reward_gb=1),
        key,
        models.User(username="referrer", telegram_id=1, referral_code="RCODE", total_quota_bytes=5 * GB, balance=100),
        models.User(username="new_blocked", telegram_id=2, total_quota_bytes=5 * GB, balance=0),
        models.User(username="new_legacy", telegram_id=3, total_quota_bytes=5 * GB, balance=0),
    ])
    setup.commit()
    runtime.set_requested_mode(setup, registration_mode="shadow")
    setup.commit()
    headers = {"X-API-Key": key.key}
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

    def snapshot():
        """Everything a referral could write."""
        session = Session()
        try:
            users = {u.username: (int(u.balance or 0), int(u.total_quota_bytes or 0), u.referred_by_id,
                                  bool(u.referral_reward_granted))
                     for u in session.query(models.User).order_by(models.User.id)}
            return {"users": users,
                    "ledger_rows": session.query(models.LedgerEntry).count(),
                    "effects": session.execute(select(func.count()).select_from(rv.receipt_approval_effects)).scalar(),
                    "shadow_events": session.execute(
                        select(func.count()).select_from(rv.receipt_approval_shadow_events)).scalar()}
        finally:
            session.close()

    def set_mode(**values):
        session = Session()
        session.execute(rv.receipt_approval_runtime_state.update().where(
            rv.receipt_approval_runtime_state.c.id == 1).values(**values))
        session.commit()
        session.close()

    # required is requested but execution-token validation does not exist yet:
    # the effective mode is blocked.
    set_mode(registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1)
    before = snapshot()
    blocked = client.post("/api/bot/referral/apply", headers=headers, json={
        "username": "new_blocked", "referral_code": "RCODE", "approval_uuid": str(uuid.uuid4())})
    check("blocked: a referral carrying an approval answers 503 receipt_approval_blocked",
          (blocked.status_code, blocked.json().get("detail")), (503, "receipt_approval_blocked"))
    check("blocked: no reward, no referral link, no ledger row, no effect, no shadow event", snapshot(), before)
    missing = client.post("/api/bot/referral/apply", headers=headers, json={
        "username": "no_such_customer", "referral_code": "RCODE", "approval_uuid": str(uuid.uuid4())})
    check("blocked wins before the customer is looked up",
          (missing.status_code, missing.json().get("detail"), snapshot()), (503, "receipt_approval_blocked", before))

    # Without an approval this is the old referral flow; the guard does not apply.
    legacy = client.post("/api/bot/referral/apply", headers=headers, json={
        "username": "new_legacy", "referral_code": "RCODE"})
    after_legacy = snapshot()
    check("a referral WITHOUT an approval is not gated by the approval mode",
          (legacy.status_code, legacy.json().get("ok"), after_legacy["users"]["new_legacy"][2:],
           after_legacy["users"]["referrer"][0]), (200, True, (1, True), 3100))

    # Back in shadow the same request that was refused goes through.
    set_mode(registration_mode="shadow", loyalty_timing_mode="legacy_activation", completeness_generation=0)
    allowed = client.post("/api/bot/referral/apply", headers=headers, json={
        "username": "new_blocked", "referral_code": "RCODE", "approval_uuid": str(uuid.uuid4())})
    after_allowed = snapshot()
    check("shadow: the referral that was refused is applied once the mode allows it",
          (allowed.status_code, allowed.json().get("ok"), after_allowed["users"]["new_blocked"][2:],
           after_allowed["users"]["referrer"][0]), (200, True, (1, True), 6100))
    check("no attempt to reach a node or any other host", _no_network.attempts, [])
    engine.dispose()

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
