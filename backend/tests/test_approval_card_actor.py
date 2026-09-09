"""telegram_bot/handlers/admin_pending.py's Approve/Reject button, for a
non-admin acting purely via a payment card's approval_telegram_id.

Run:  python3 backend/tests/test_approval_card_actor.py

Bug reported 2026-09-08, the day after the per-card approval id feature
(models.PaymentCard.approval_telegram_id) shipped: "the customer's receipt
now reaches its admin, but tapping تایید/رد (Approve/Reject) does nothing at
all." Root cause: only the NOTIFICATION half of that feature was wired up
(telegram_bot/handlers/customer.py's _notify_targets correctly sends the
receipt+buttons to the card's approval id even when that id is not a
recognized admin) - but admin_pending.py's ENTIRE router was gated by a
router-level `_is_admin_filter`, which a non-admin id fails. aiogram's
behaviour when a router-level filter rejects an update is to just try other
routers/handlers - nothing calls answerCallbackQuery, so the tap looked to
the user exactly like a dead button: no toast, no spinner, no error, no log
line, nothing.

Fixed by moving JUST the Approve/Reject callback onto its own router
(admin_pending.approval_router, not gated by _is_admin_filter) with its own
two-path authorization check (_approval_actor): a recognized admin, as
before, OR a Telegram id that matches the approval_telegram_id of the
SPECIFIC card this ONE pending request's receipt was shown against - which
grants nothing beyond that single button tap (no list, no history, no other
admin screen).

This drives _approval_actor and the real cb_approval coroutine directly
(no live bot needed), with panel_bridge.api, admin_scope.resolve_admin_scope
and storage all faked."""
from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app.telegram_bot.handlers import admin_pending  # noqa: E402
from app.telegram_bot.callbacks import ApprovalCB  # noqa: E402


class FakeMessage:
    def __init__(self, text="سرویس‌های در انتظار", photo=None):
        self.text = text
        self.caption = None
        self.photo = photo
        self.edits: list[str] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append(text)

    async def edit_caption(self, caption, reply_markup=None):
        self.edits.append(caption)


class FakeCall:
    def __init__(self, from_user_id, message=None):
        self.from_user = type("U", (), {"id": from_user_id})()
        self.message = message or FakeMessage()
        self.answers: list[tuple] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


class FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


ADMIN_SCOPE = {
    "owner_admin_id": 1, "role": "superadmin", "is_full_admin": True,
    "is_unscoped": False, "username": "root", "owner_ids": {1}, "include_unowned": True,
}

PENDING_ROW = {
    "id": 42, "status": "pending", "kind": "topup", "telegram_id": 555,
    "telegram_username": "cust1", "telegram_name": "Cust One",
    "target_username": "cust1", "price": 100000, "final_price": None,
    "payment_card_id": 7, "owner_admin_id": None,
}

CARD_WITH_APPROVAL_ID = {"id": 7, "approval_telegram_id": 999888}
CARD_NO_APPROVAL_ID = {"id": 7, "approval_telegram_id": None}


async def run():
    real_resolve = admin_pending.resolve_admin_scope
    real_api = admin_pending.api
    real_storage_get = admin_pending.storage.get_pending
    real_storage_claim = admin_pending.storage.claim_pending
    real_storage_set_status = admin_pending.storage.set_status
    real_storage_may_handle = admin_pending.storage.may_handle

    try:
        # ------------------------------------------------- _approval_actor
        print("--- a recognized admin is allowed, and marked as full admin ---")
        admin_pending.resolve_admin_scope = AsyncMock(return_value=ADMIN_SCOPE)
        admin_pending.storage.get_pending = lambda rid: PENDING_ROW
        allowed, is_full_admin, scope = await admin_pending._approval_actor(42, 111)
        check("allowed", allowed, True)
        check("is_full_admin", is_full_admin, True)
        check("scope returned", scope, ADMIN_SCOPE)

        print("\n--- NOT a recognized admin, but matches this request's card's approval id -> allowed ---")
        admin_pending.resolve_admin_scope = AsyncMock(return_value=None)
        admin_pending.api = type("A", (), {"get_payment_card": AsyncMock(return_value=CARD_WITH_APPROVAL_ID)})()
        allowed2, is_full_admin2, scope2 = await admin_pending._approval_actor(42, 999888)
        check("allowed via card match", allowed2, True)
        check("not treated as full admin", is_full_admin2, False)
        check("no scope for a card-only actor", scope2, None)

        print("\n--- NOT a recognized admin, and does NOT match the card's approval id -> refused ---")
        allowed3, _, _ = await admin_pending._approval_actor(42, 12345)
        check("refused", allowed3, False)

        print("\n--- card has no approval_telegram_id set at all -> refused (unchanged from before this feature) ---")
        admin_pending.api = type("A", (), {"get_payment_card": AsyncMock(return_value=CARD_NO_APPROVAL_ID)})()
        allowed4, _, _ = await admin_pending._approval_actor(42, 999888)
        check("refused - nothing configured", allowed4, False)

        print("\n--- pending request has no payment_card_id at all -> refused for a non-admin, no crash ---")
        admin_pending.storage.get_pending = lambda rid: {**PENDING_ROW, "payment_card_id": None}
        allowed5, _, _ = await admin_pending._approval_actor(42, 999888)
        check("refused, no crash", allowed5, False)

        print("\n--- pending request id does not exist at all -> refused for a non-admin, no crash ---")
        admin_pending.storage.get_pending = lambda rid: None
        allowed6, _, _ = await admin_pending._approval_actor(999999, 999888)
        check("refused, no crash", allowed6, False)

        # ---------------------------------------------------- cb_approval
        print("\n--- full flow: the card-only actor's tap actually approves the request ---")
        admin_pending.storage.get_pending = lambda rid: dict(PENDING_ROW)
        admin_pending.api = type("A", (), {
            "get_payment_card": AsyncMock(return_value=CARD_WITH_APPROVAL_ID),
            "add_balance": AsyncMock(return_value={"balance": 100000}),
            "record_card_payment": AsyncMock(return_value=None),
        })()
        claimed = {}
        admin_pending.storage.claim_pending = lambda rid: claimed.setdefault(rid, True) or True
        set_statuses = []
        admin_pending.storage.set_status = lambda rid, status: set_statuses.append((rid, status))
        admin_pending.storage.may_handle = lambda *a, **k: (_ for _ in ()).throw(AssertionError("may_handle must NOT be called for a card-only actor"))

        call = FakeCall(from_user_id=999888)
        bot = FakeBot()
        await admin_pending.cb_approval(call, ApprovalCB(action="approve", request_id=42), bot)
        check("request actually got claimed", claimed.get(42), True)
        check("request marked approved", ("approved" in [s[1] for s in set_statuses]), True)
        check("no 'access denied' answer along the way", any(a[0] == "دسترسی ندارید" for a in call.answers), False)
        check("final answer is a success toast", call.answers[-1][0], "تایید شد")

        print("\n--- full flow: an unrelated stranger's tap is refused, nothing is touched ---")
        claimed.clear()
        set_statuses.clear()
        admin_pending.resolve_admin_scope = AsyncMock(return_value=None)
        admin_pending.storage.get_pending = lambda rid: dict(PENDING_ROW)
        stranger_call = FakeCall(from_user_id=1010101)
        await admin_pending.cb_approval(stranger_call, ApprovalCB(action="approve", request_id=42), FakeBot())
        check("refused with an alert", stranger_call.answers[-1], ("دسترسی ندارید", True))
        check("nothing was ever claimed", claimed, {})
        check("status never changed", set_statuses, [])

    finally:
        admin_pending.resolve_admin_scope = real_resolve
        admin_pending.api = real_api
        admin_pending.storage.get_pending = real_storage_get
        admin_pending.storage.claim_pending = real_storage_claim
        admin_pending.storage.set_status = real_storage_set_status
        admin_pending.storage.may_handle = real_storage_may_handle


asyncio.run(run())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
