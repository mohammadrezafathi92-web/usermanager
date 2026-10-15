"""P5a finding 1's subject - an approval-carrying wallet DEBIT - no longer
exists (Receipt Void P7): the bot's add-balance takes a stated reason
(source_kind), and a negative amount is only an admin's manual adjustment,
which never carries a receipt approval. What must hold now:

  * no source_kind -> 422 for every caller, nothing written;
  * a negative amount with an approval (or a card) -> 422, nothing written;
  * a negative manual adjustment needs the admin's Telegram id, a reason
    and the caller's X-Idempotency-Key (none -> 422, nothing written); it
    takes exactly its amount, once per key - a resend after a lost answer
    changes nothing;
  * a receipt top-up cannot be negative;
  * only a MANAGED bot (in-process, or the panel's remote installation key)
    may name the adjusting admin; an integration key or an old global key
    that names the superadmin is refused, nothing is booked.
SQLite.
"""
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
DIRECTORY = tempfile.mkdtemp(prefix="um-debit-p7-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
import _no_network
_no_network.install([])

from fastapi import HTTPException

from sqlalchemy import select

from app import models, models_receipt_void as rv, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router
from app.services import bot_auth, receipt_void_schema, wallet_accounts

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schema ready", receipt_void_schema.bootstrap(engine, SessionLocal)["ready"], True)
with SessionLocal() as db:
    db.add(models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000))
    wallet_accounts.create_user_with_wallet(db, username="payer", telegram_id=501, balance=1000)
    db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)


def call(key=None, **body):
    if key is None and body.get("source_kind") == "manual_adjustment":
        key = str(uuid.uuid4())            # a fresh attempt; key="" is "no key at all"
    with SessionLocal() as db:
        try:
            return bot_router.add_balance("payer", schemas.BotAddBalanceRequest(**body), db=db, principal=PRINCIPAL,
                                          x_idempotency_key=key).balance
        except HTTPException as exc:
            return exc.status_code, exc.detail


def balance():
    with SessionLocal() as db:
        return db.query(models.User).filter_by(username="payer").one().balance


check("no source_kind: 422, nothing written", (call(amount=-100), balance()), ((422, "source_kind_required"), 1000))
check("a negative amount carrying an approval: 422, nothing written",
      (call(amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000,
            approval_uuid=str(uuid.uuid4())), balance()), ((422, "manual_adjustment_without_receipt"), 1000))
check("a manual adjustment without the admin: 422",
      call(amount=-100, source_kind="manual_adjustment", reason="r"), (422, "actor_telegram_id_required"))
check("...by a Telegram id that is no admin: 403",
      call(amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=4242),
      (403, "adjusting_admin_not_found"))
check("...without a reason: 422",
      call(amount=-100, source_kind="manual_adjustment", actor_telegram_id=1000),
      (422, "wallet_adjustment_reason_required"))
check("a manual adjustment with no X-Idempotency-Key: 422 before anything, nothing written",
      (call("", amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000), balance()),
      ((422, "idempotency_key_required"), 1000))
key = str(uuid.uuid4())
check("a manual debit by the admin with a reason takes exactly its amount",
      call(key, amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000), 900)
check("...its answer lost, the bot resends the same key: applied once",
      (call(key, amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000), balance()),
      (900, 900))
check("...the same key with another amount: 409",
      call(key, amount=-200, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000),
      (409, "wallet_request_changed"))
check("an overdraft is refused and changes nothing",
      (call(amount=-5000, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000)[0], balance()),
      (400, 900))
check("a receipt top-up cannot be negative", call(amount=-1, source_kind="receipt_topup"),
      (422, "receipt_topup_amount_invalid"))
# ------------------------------------------------------------------ who may name the adjusting admin
with SessionLocal() as db:
    owner = models.AdminUser(username="tenant", hashed_password="x", telegram_id=2000)
    db.add(owner)
    db.flush()
    db.query(models.User).filter_by(username="payer").update({"owner_admin_id": owner.id})
    keys = {kind: models.ApiKey(key=f"k-{kind}", label=kind, key_type=kind,
                                owner_admin_id=owner.id if kind == bot_auth.KeyType.TENANT_INTEGRATION else None,
                                scope_enforced=kind == bot_auth.KeyType.TENANT_INTEGRATION,
                                capabilities=bot_auth.serialize_capabilities(
                                    [bot_auth.WALLET_WRITE, bot_auth.CUSTOMER_READ])
                                if kind == bot_auth.KeyType.TENANT_INTEGRATION else None)
            for kind in (bot_auth.KeyType.TENANT_INTEGRATION, bot_auth.KeyType.LEGACY_GLOBAL,
                         bot_auth.KeyType.REMOTE_SHARED_BOT)}
    db.add_all(keys.values())
    db.commit()
    principals = {kind: bot_auth.BotPrincipal.from_api_key(db.get(models.ApiKey, key.id)) for kind, key in keys.items()}


def by(principal, actor=1000):
    with SessionLocal() as db:
        try:
            return bot_router.add_balance("payer", schemas.BotAddBalanceRequest(
                amount=-50, source_kind="manual_adjustment", reason="spoof", actor_telegram_id=actor),
                db=db, principal=principal, x_idempotency_key=str(uuid.uuid4())).balance
        except HTTPException as exc:
            return exc.status_code, exc.detail


before = balance()
check("an integration key naming the superadmin as actor: 403, nothing written",
      (by(principals[bot_auth.KeyType.TENANT_INTEGRATION]), balance()),
      ((403, "manual_adjustment_requires_managed_bot"), before))
check("an old global key naming the superadmin as actor: 403, nothing written",
      (by(principals[bot_auth.KeyType.LEGACY_GLOBAL]), balance()),
      ((403, "manual_adjustment_requires_managed_bot"), before))
with SessionLocal() as db:
    rows = db.execute(select(rv.wallet_operations.c.actor_id).where(
        rv.wallet_operations.c.operation_type == "wallet_manual_adjustment")).scalars().all()
check("...no adjustment was booked to anyone by them", len(rows), 1)
check("the panel's managed remote bot attests the admin: accepted",
      by(principals[bot_auth.KeyType.REMOTE_SHARED_BOT]), before - 50)

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
