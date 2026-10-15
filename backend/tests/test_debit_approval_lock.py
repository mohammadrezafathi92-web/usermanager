"""P5a finding 1's subject - an approval-carrying wallet DEBIT - no longer
exists (Receipt Void P7): the bot's add-balance takes a stated reason
(source_kind), and a negative amount is only an admin's manual adjustment,
which never carries a receipt approval. What must hold now:

  * no source_kind -> 422 for every caller, nothing written;
  * a negative amount with an approval (or a card) -> 422, nothing written;
  * a negative manual adjustment needs the admin's Telegram id and a
    reason; with them it takes exactly its amount, once per key;
  * a receipt top-up cannot be negative.
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

from app import models, schemas
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
key = str(uuid.uuid4())
check("a manual debit by the admin with a reason takes exactly its amount",
      call(key, amount=-100, source_kind="manual_adjustment", reason="r", actor_telegram_id=1000), 900)
check("...the same key again: applied once",
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
check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
