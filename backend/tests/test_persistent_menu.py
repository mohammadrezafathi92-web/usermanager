"""The always-visible bottom menu bar, and that it runs the SAME code as the
inline buttons.

Run:  python3 backend/tests/test_persistent_menu.py

Requested 2026-09-13: "اون دکمه‌ها پایین ثابت هست این خیلی جذابه". An inline
keyboard is attached to one message and is gone the moment the customer
scrolls; a ReplyKeyboardMarkup sits under the text box forever, which for a
shop is the difference between a bot people open and one they forget.

The risk in bolting a second entry point onto an existing flow is that the
two drift - «خرید اشتراک» doing one thing from the bar and another from the
message. So the bar deliberately has no handlers of its own: a tap is
dressed up as the inline press those handlers already take. These tests
pin down that wiring, the shared item list, and the two ordering rules the
bar depends on.
"""
from __future__ import annotations

import asyncio
import inspect
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


from app.telegram_bot import keyboards  # noqa: E402
from app.telegram_bot.handlers import persistent_menu, customer, tutorials  # noqa: E402
from app.telegram_bot.handlers import build_router  # noqa: E402


class FakeMessage:
    def __init__(self, text="", user_id=555):
        self.text = text
        self.from_user = type("U", (), {"id": user_id, "username": "c", "full_name": "C"})()
        self.answers: list[tuple] = []

    async def answer(self, text, reply_markup=None, **kw):
        self.answers.append((text, reply_markup))
        return self


async def run():
    print("--- one list of items feeds both menus ---")
    # Optimization #2 (2026-09-23): the bar is now ONE combined
    # ReplyKeyboardMarkup for admins/sellers too (see keyboards.
    # persistent_menu_kb's docstring for why a separate admin-only bar was
    # rejected), so _LABEL_TO_ACTION covers all three item lists, not just
    # the customer one - admin_list appears in both admin lists under
    # different label text ("...کاربران" vs "...کاربران من"), so the set of
    # ACTIONS still has no duplicates even though the label count is one
    # bigger than the action count.
    all_items = [
        *keyboards.CUSTOMER_MENU_ITEMS, *keyboards.ADMIN_MENU_ITEMS_FULL, *keyboards.ADMIN_MENU_ITEMS_SELLER,
    ]
    check("every bar label comes from one of the three item lists",
          sorted(set(persistent_menu._LABEL_TO_ACTION.values())),
          sorted({a for a, _ in all_items}))
    check("and every item has a handler behind it",
          [a for a, _ in all_items if a not in persistent_menu._ACTIONS], [])

    print("\n--- the bar is built from the same on/off switches ---")
    keyboards_api = keyboards.__dict__
    from app.telegram_bot import panel_bridge
    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(return_value=[])
    kb = await keyboards.persistent_menu_kb()
    labels = [b.text for row in kb.keyboard for b in row]
    check("all ten items when nothing is disabled", len(labels), len(keyboards.CUSTOMER_MENU_ITEMS))
    check("two per row", all(len(r) <= 2 for r in kb.keyboard), True)
    check("it resizes instead of taking half the screen", kb.resize_keyboard, True)
    check("and stays open", kb.is_persistent, True)

    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(
        return_value=["cust_topup", "cust_myid", "cust_link"])
    kb2 = await keyboards.persistent_menu_kb()
    labels2 = [b.text for row in kb2.keyboard for b in row]
    check("a switched-off item is not on the bar either",
          any("افزایش اعتبار" in l for l in labels2), False)
    check("...and the rest are still there", len(labels2), len(keyboards.CUSTOMER_MENU_ITEMS) - 3)

    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(
        return_value=[a for a, _ in keyboards.CUSTOMER_MENU_ITEMS])
    check("everything off means no bar at all, not an empty one",
          await keyboards.persistent_menu_kb(), None)
    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(return_value=[])

    print("\n--- a tap runs the inline handler, unchanged ---")
    persistent_menu.resolve_admin_scope = AsyncMock(return_value=None)  # a customer
    called = {}

    async def fake_buy(call, state=None):
        called["buy"] = call

    persistent_menu._ACTIONS["cust_buy"] = (fake_buy, ("state",))
    msg = FakeMessage("🛒 خرید اکانت جدید")
    state = AsyncMock()
    await persistent_menu.on_menu_tap(msg, state=state, bot=object())
    check("the matching handler ran", "buy" in called, True)
    check("...and the FSM state was cleared on the way", state.clear.await_count, 1)

    print("\n--- the shim looks enough like an inline press ---")
    tap = called["buy"]
    check("it carries the real user", tap.from_user.id, 555)
    await tap.message.edit_text("سلام")
    check("an edit becomes a new message, since there is nothing to edit",
          msg.answers[-1][0], "سلام")
    await tap.answer()  # the bare acknowledgement
    check("a wordless call.answer() says nothing", len(msg.answers), 1)
    await tap.answer("خطا", show_alert=True)
    check("an ALERT becomes a real message or it is lost", msg.answers[-1][0], "خطا")
    # customer.py's _resolve_account checks isinstance(target, CallbackQuery)
    # and, finding this is not one, calls answer() in the MESSAGE sense.
    # Swallowing that made the multi-account picker disappear.
    await tap.answer("کدام حساب؟", reply_markup="KB")
    check("...and so does a message-style answer with a keyboard",
          msg.answers[-1], ("کدام حساب؟", "KB"))

    print("\n--- an admin tapping it gets the customer screen, never silence ---")
    # Reported the day the bar shipped: "این دکمه ها کار نمیکنه". The first
    # version returned early for anyone with an admin scope, so every tap
    # vanished with no reply. The bar is pinned to a CHAT, not a role, so
    # whoever has it must get an answer from it.
    called.clear()
    await persistent_menu.on_menu_tap(FakeMessage("🛒 خرید اکانت جدید"), state=AsyncMock(), bot=object())
    check("an admin gets the same handler, not nothing", "buy" in called, True)

    print("\n--- a disabled item cannot be reached by typing its label ---")
    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(return_value=["cust_buy"])
    called.clear()
    await persistent_menu.on_menu_tap(FakeMessage("🛒 خرید اکانت جدید"), state=AsyncMock(), bot=object())
    check("switched off means off, however it is reached", called, {})
    panel_bridge.api.get_customer_menu_disabled_items = AsyncMock(return_value=[])

    print("\n--- ordinary typing still falls through ---")
    called.clear()
    await persistent_menu.on_menu_tap(FakeMessage("mohammad1234"), state=AsyncMock(), bot=object())
    check("a username is not a menu tap", called, {})

    print("\n--- an admin item tap resolves and passes acting_scope ---")
    seller_scope = {"owner_admin_id": 9, "role": "seller", "is_full_admin": False,
                     "is_unscoped": False, "username": "s", "owner_ids": {9},
                     "include_unowned": False, "sell_block_reason": None}
    persistent_menu.resolve_admin_scope = AsyncMock(return_value=seller_scope)

    async def fake_admin_create(call, state=None, acting_scope=None):
        called["admin_create"] = acting_scope

    persistent_menu._ACTIONS["admin_create"] = (fake_admin_create, ("state", "acting_scope"))
    called.clear()
    await persistent_menu.on_menu_tap(FakeMessage("➕ ساخت کاربر"), state=AsyncMock(), bot=object())
    check("the handler got the freshly-resolved scope", called.get("admin_create"), seller_scope)

    print("\n--- a full-admin-only item refuses a seller, not silently ---")
    called.clear()
    msg = FakeMessage("📢 پیام همگانی")
    await persistent_menu.on_menu_tap(msg, state=AsyncMock(), bot=object())
    check("the broadcast handler did not run", "admin_broadcast" in called, False)
    check("...and the seller was told why, not left with silence",
          any("مخصوص مدیران" in a[0] for a in msg.answers), True)

    print("\n--- a non-admin typing an admin label also gets a refusal ---")
    persistent_menu.resolve_admin_scope = AsyncMock(return_value=None)  # not an admin at all
    called.clear()
    msg = FakeMessage("➕ ساخت کاربر")
    await persistent_menu.on_menu_tap(msg, state=AsyncMock(), bot=object())
    check("admin_create did not run for a plain customer", "admin_create" in called, False)
    check("...they were told it's admin-only, not left with silence",
          any("مخصوص مدیران" in a[0] for a in msg.answers), True)


asyncio.run(run())

print("\n--- the bar is registered BEFORE the customer FSM handlers ---")
# Otherwise a tap mid-flow is swallowed as the username/amount the customer
# was being asked for, and the bar silently stops working exactly when
# someone is stuck and most needs it.
names = [r.name for r in build_router().sub_routers]
check("persistent_menu comes first", names.index("persistent_menu") < names.index("customer"), True)

print("\n--- /start puts the bar in place for a customer ---")
from app.telegram_bot.handlers import start as start_handlers  # noqa: E402

src = inspect.getsource(start_handlers.cmd_start)
check("/start sends it", "send_menu_bar" in src, True)
# Deliberately NOT gated on role any more: the bar belongs to a chat, so
# restricting who receives it only creates chats where it is present and
# does nothing.
check("...to everyone, so nobody ends up with a bar that does nothing",
      "scope is None" in src, False)

print("\n--- the plan cards ---")
# "کارت پلن با آیکون و توضیح و لیست تخفیف‌ها، و پیام‌های مرتب‌تر" - the picker
# used to be a bare "یک پکیج انتخاب کنید:" over buttons reading "name ·
# price", so everything needed to CHOOSE was either crammed into the name or
# missing.
from app.telegram_bot.utils import fmt_gb, fmt_toman, fmt_days, package_card, packages_message  # noqa: E402

check("prices are in Persian digits", fmt_toman(100000), "۱۰۰,۰۰۰ تومان")
check("free is said, not printed as zero", fmt_toman(0), "رایگان")
check("gigabytes", fmt_gb(20), "۲۰ گیگابایت")
check("a sub-gigabyte package reads as megabytes, not 0.2", fmt_gb(0.2), "۲۰۵ مگابایت")
check("no quota means unlimited, not zero", fmt_gb(0), "نامحدود")
check("days", fmt_days(30), "۳۰ روز")
check("no duration says so", fmt_days(0), "بدون انقضا")

rich = {"name": "پریمیوم", "price": 250000, "quota_gb": 50, "duration_days": 30,
        "max_concurrent_sessions": 2, "one_time_per_user": False,
        "connections": [{"protocol": "wireguard"}, {"protocol": "xray"}, {"protocol": "wireguard"}],
        "description": "مناسب تماس تصویری."}
card = package_card(rich, index=1)
for label, needle in [("the name", "پریمیوم"), ("the price", "۲۵۰,۰۰۰"),
                      ("the quota", "۵۰ گیگابایت"), ("the duration", "۳۰ روز"),
                      ("how many devices", "کاربر همزمان"),
                      ("what they actually get", "WireGuard"),
                      ("the admin's own description", "مناسب تماس تصویری.")]:
    check(f"the card shows {label}", needle in card, True)
check("a protocol bundled twice is listed once", card.count("WireGuard"), 1)

bare = package_card({"name": "ساده", "price": 50000})
check("an empty description adds no empty line", bare.strip().endswith("بدون انقضا"), True)
check("no session line when the admin did not set one", "کاربر همزمان" in bare, False)
check("a one-time package says so",
      "فقط یک‌بار" in package_card({"name": "تست", "one_time_per_user": True}), True)

msg = packages_message([rich, {"name": "ساده", "price": 50000}])
check("cards are numbered to match the buttons", "۱." in msg and "۲." in msg, True)
from app.telegram_bot.utils import CARD_SEPARATOR  # noqa: E402
check("two cards get exactly one divider between them",
      msg.count(CARD_SEPARATOR), 1)

from app.telegram_bot.keyboards import packages_kb  # noqa: E402
kb = packages_kb([dict(rich, id=1), {"id": 2, "name": "ساده", "price": 50000}], "new")
labels = [b.text for row in kb.inline_keyboard for b in row]
check("the buttons carry the same numbers", labels[0].startswith("۱."), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("منوی ثابت پایین چت کار می‌کند و همان کد دکمه‌های داخل پیام را اجرا می‌کند")
