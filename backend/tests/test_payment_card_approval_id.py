"""Per-card approval Telegram id (models.PaymentCard.approval_telegram_id).

Run:  python3 backend/tests/test_payment_card_approval_id.py

Feature requested 2026-09-08: alongside each registered card-to-card
payment card, an admin can now set a numeric Telegram id that should ALSO
be notified (in addition to the normal approval targets) whenever a
customer's receipt paid to THAT specific card comes in for approval - lets
different cards be watched by different people instead of every receipt
always landing with the same admin(s).

Covers: the field round-trips through create/update (routers/panel_
settings.py, both the global pool and a per-admin "my payment" pool); the
new GET /api/bot/payment-cards/{id} lookup (routers/bot.py) used to find
the EXACT card a pending request was shown, not whichever one the pool
currently considers active; panel_bridge.PanelBridge.get_payment_card's
None-on-404 contract; and telegram_bot/handlers/customer.py's
_notify_targets, which must ADD the card's approval id to the existing
approval-targets set, never replace it, and must be a no-op when the card
has none set or the request carries no payment_card_id at all (e.g. a
'link' request, no payment involved)."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from fastapi import HTTPException  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import panel_settings, bot as bot_router  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

print("--- create_payment_card persists approval_telegram_id (global pool) ---")
card = panel_settings.create_payment_card(
    schemas.PaymentCardCreate(card_number="6037-0000-0000-0001", approval_telegram_id=555111),
    db=db,
)
check("stored on create", card.approval_telegram_id, 555111)

print("\n--- a card created with no approval id defaults to None (unchanged behaviour) ---")
card2 = panel_settings.create_payment_card(schemas.PaymentCardCreate(card_number="6037-0000-0000-0002"), db=db)
check("defaults to None", card2.approval_telegram_id, None)

print("\n--- update_payment_card can set/clear it on an existing card ---")
panel_settings.update_payment_card(card2.id, schemas.PaymentCardUpdate(approval_telegram_id=999222), db=db)
db.refresh(card2)
check("set via update", card2.approval_telegram_id, 999222)
panel_settings.update_payment_card(card2.id, schemas.PaymentCardUpdate(approval_telegram_id=None), db=db)
db.refresh(card2)
check("cleared via update", card2.approval_telegram_id, None)

print("\n--- GET /api/bot/payment-cards/{id} returns the exact card by id ---")
out = bot_router.get_payment_card(card.id, db=db)
# Called directly (bypassing FastAPI's routing layer, which normally
# converts the return value via response_model=PaymentCardOut) - so this
# is still the raw ORM row, same as create_payment_card's return value
# above. What matters here is the field the whole feature hinges on.
check("returns the right card", out.id, card.id)
check("...with the right approval id", out.approval_telegram_id, 555111)

print("\n--- ...and 404s for a card that doesn't exist ---")
try:
    bot_router.get_payment_card(999999, db=db)
    check("raises 404", False, True)
except HTTPException as exc:
    check("raises 404", exc.status_code, 404)

print("\n--- panel_bridge.PanelBridge.get_payment_card converts the bare ORM row ---")
# Regression: get_payment_card (like create/update_payment_card elsewhere
# in this codebase) returns a bare ORM PaymentCard row - a real HTTP call
# gets that auto-converted via response_model=PaymentCardOut, but
# panel_bridge.py's _call() bypasses FastAPI's routing layer entirely (see
# that module's docstring), and its generic _dump() helper only converts
# objects that ALREADY have a .model_dump() method (i.e. are already
# pydantic schema objects) - a bare ORM row would silently pass through
# unconverted, crashing the very first dict-style access a caller (here,
# telegram_bot/handlers/customer.py's _notify_targets) does on it. This
# exercises the exact conversion line get_payment_card relies on, using
# the same `card` ORM row created above.
from app.telegram_bot import panel_bridge as panel_bridge_module  # noqa: E402

dumped_card = panel_bridge_module._dump(schemas.PaymentCardOut.model_validate(card))
check("bare ORM row is converted to a plain dict", isinstance(dumped_card, dict), True)
check("...with the right id", dumped_card["id"], card.id)
check("...with the right approval id", dumped_card["approval_telegram_id"], 555111)

print("\n--- per-admin ('my payment') pool works exactly the same way ---")
admin = models.AdminUser(username="reseller1", hashed_password="x", is_superadmin=False)
db.add(admin)
db.commit()
db.refresh(admin)
own_card = panel_settings.create_my_payment_card(
    schemas.PaymentCardCreate(card_number="6219-0000-0000-0003", approval_telegram_id=777333),
    admin=admin, db=db,
)
check("owner_admin_id set correctly", own_card.owner_admin_id, admin.id)
check("approval id persisted on the per-admin pool too", own_card.approval_telegram_id, 777333)


# ------------------------------------------------- _notify_targets routing
from app.telegram_bot.handlers import customer as customer_handlers  # noqa: E402
from app.telegram_bot.config import config  # noqa: E402


class FakeApi:
    """Stands in for panel_bridge.api - only the two methods
    _notify_targets actually calls."""
    def __init__(self, admin_tg=None, cards=None):
        self.admin_tg = admin_tg or {}
        self.cards = cards or {}

    async def get_admin_telegram_id(self, admin_id):
        return self.admin_tg.get(admin_id)

    async def get_payment_card(self, card_id):
        return self.cards.get(card_id)


async def run_notify_tests():
    real_api = customer_handlers.api
    real_targets = config.approval_targets
    try:
        config.approval_targets = lambda: {111}

        print("\n--- a card WITH an approval id ADDS it to the normal targets ---")
        customer_handlers.api = FakeApi(cards={7: {"id": 7, "approval_telegram_id": 42424242}})
        targets = await customer_handlers._notify_targets({"payment_card_id": 7})
        check("normal target still present", 111 in targets, True)
        check("card's approval id added too", 42424242 in targets, True)
        check("exactly these two, nothing extra", targets, {111, 42424242})

        print("\n--- a card with NO approval id set changes nothing ---")
        customer_handlers.api = FakeApi(cards={8: {"id": 8, "approval_telegram_id": None}})
        targets2 = await customer_handlers._notify_targets({"payment_card_id": 8})
        check("only the normal target", targets2, {111})

        print("\n--- no payment_card_id at all (e.g. a 'link' request) is a no-op, no crash ---")
        customer_handlers.api = FakeApi()
        targets3 = await customer_handlers._notify_targets({"owner_admin_id": None})
        check("only the normal target, no crash", targets3, {111})

        print("\n--- a payment_card_id for a card that no longer exists is a no-op, no crash ---")
        customer_handlers.api = FakeApi(cards={})
        targets4 = await customer_handlers._notify_targets({"payment_card_id": 999})
        check("only the normal target, no crash", targets4, {111})

        print("\n--- combines correctly with the existing owner-telegram-id addition ---")
        customer_handlers.api = FakeApi(
            admin_tg={5: 606060}, cards={9: {"id": 9, "approval_telegram_id": 707070}},
        )
        targets5 = await customer_handlers._notify_targets({"owner_admin_id": 5, "payment_card_id": 9})
        check("has the normal target, the owner's, and the card's", targets5, {111, 606060, 707070})
    finally:
        customer_handlers.api = real_api
        config.approval_targets = real_targets


asyncio.run(run_notify_tests())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
