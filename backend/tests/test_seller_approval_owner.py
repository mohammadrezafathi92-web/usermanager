"""telegram_bot/handlers/admin_pending.py's _approval_actor - a Seller who
owns the customer a pending request belongs to must be allowed to act on
THAT request's Approve/Reject buttons, same as they're already (legitimately)
shown them by customer.py's _notify_targets.

Run:  python3 backend/tests/test_seller_approval_owner.py

Bug reported 2026-09-09: "فروشندم روی تایید میزنه این میاد" (my Seller taps
Approve, this [دسترسی ندارید] comes up). Root cause: _notify_targets sends
the request's owner_admin_id's linked Telegram id a copy of the receipt
regardless of role (a Seller's own customer's owner_admin_id IS the Seller),
but _approval_actor only ever authorized a full admin (superadmin/Admin -
resolve_admin_scope's is_full_admin excludes ROLE_SELLER) or an exact
PaymentCard.approval_telegram_id match - neither covers "the Seller who
owns this specific customer". Fix adds a third path: scope.owner_admin_id
== pending.owner_admin_id, narrowly scoped to just that one request (same
guarantee as the existing card-approval-id path)."""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app.telegram_bot import storage  # noqa: E402
from app.telegram_bot.config import config  # noqa: E402
from app.telegram_bot.handlers import admin_pending  # noqa: E402

# Isolated sqlite file for the bot's own pending-request store - never the
# real /app/data/bot_data.db.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
config.db_path = _tmp.name
storage.init_db()

SELLER_TG_ID = 111
OTHER_SELLER_TG_ID = 222
FULL_ADMIN_TG_ID = 333
CARD_APPROVER_TG_ID = 444
STRANGER_TG_ID = 555

SELLER_ADMIN_ID = 11
OTHER_SELLER_ADMIN_ID = 22

# Fake package dict, just enough for create_pending.
_pkg = {"id": 1, "name": "پکیج تست", "quota_gb": 20, "duration_days": 30}

request_id = storage.create_pending(
    telegram_id=999999,
    telegram_username="customer1",
    telegram_name="مشتری",
    kind="new",
    package=_pkg,
    target_username="customer1",
    owner_admin_id=SELLER_ADMIN_ID,
)


class _FakeScope(dict):
    pass


async def _fake_resolve_admin_scope(tg_id: int):
    if tg_id == SELLER_TG_ID:
        return {"owner_admin_id": SELLER_ADMIN_ID, "role": "seller", "is_full_admin": False,
                "is_unscoped": False, "username": "seller1", "owner_ids": {SELLER_ADMIN_ID}, "include_unowned": False}
    if tg_id == OTHER_SELLER_TG_ID:
        return {"owner_admin_id": OTHER_SELLER_ADMIN_ID, "role": "seller", "is_full_admin": False,
                "is_unscoped": False, "username": "seller2", "owner_ids": {OTHER_SELLER_ADMIN_ID}, "include_unowned": False}
    if tg_id == FULL_ADMIN_TG_ID:
        return {"owner_admin_id": 1, "role": "admin", "is_full_admin": True,
                "is_unscoped": False, "username": "admin1", "owner_ids": {1, SELLER_ADMIN_ID}, "include_unowned": False}
    return None


async def _fake_get_payment_card(card_id):
    return {"id": card_id, "approval_telegram_id": CARD_APPROVER_TG_ID}


async def main():
    admin_pending.resolve_admin_scope = _fake_resolve_admin_scope
    admin_pending.api.get_payment_card = _fake_get_payment_card

    print("--- the Seller who OWNS this customer is allowed, scoped to this one request ---")
    allowed, is_full_admin, scope = await admin_pending._approval_actor(request_id, SELLER_TG_ID)
    check("allowed", allowed, True)
    check("is_full_admin False (narrow scope, not real admin access)", is_full_admin, False)
    check("no broader scope object handed back", scope, None)

    print("\n--- a DIFFERENT Seller (does not own this customer) is refused ---")
    allowed, is_full_admin, scope = await admin_pending._approval_actor(request_id, OTHER_SELLER_TG_ID)
    check("refused", allowed, False)

    print("\n--- a full admin is still allowed as before ---")
    allowed, is_full_admin, scope = await admin_pending._approval_actor(request_id, FULL_ADMIN_TG_ID)
    check("allowed", allowed, True)
    check("is_full_admin True", is_full_admin, True)

    print("\n--- an unrecognized Telegram id with no scope at all is refused (no card on this request) ---")
    allowed, is_full_admin, scope = await admin_pending._approval_actor(request_id, STRANGER_TG_ID)
    check("refused", allowed, False)

    print("\n--- pre-existing behaviour untouched: card approval_telegram_id still works on a card-bound request ---")
    card_request_id = storage.create_pending(
        telegram_id=999998, telegram_username="customer2", telegram_name="مشتری۲",
        kind="new", package=_pkg, target_username="customer2",
        owner_admin_id=SELLER_ADMIN_ID, payment_card_id=77,
    )
    allowed, is_full_admin, scope = await admin_pending._approval_actor(card_request_id, CARD_APPROVER_TG_ID)
    check("allowed via card match", allowed, True)
    check("is_full_admin False", is_full_admin, False)


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
