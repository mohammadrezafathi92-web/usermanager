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
    check("every bar label comes from CUSTOMER_MENU_ITEMS",
          sorted(persistent_menu._LABEL_TO_ACTION.values()),
          sorted(a for a, _ in keyboards.CUSTOMER_MENU_ITEMS))
    check("and every item has a handler behind it",
          [a for a, _ in keyboards.CUSTOMER_MENU_ITEMS if a not in persistent_menu._ACTIONS], [])

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
    await tap.answer()  # the silent toast
    check("a silent call.answer() says nothing", len(msg.answers), 1)
    await tap.answer("خطا", show_alert=True)
    check("but an ALERT has to become a real message or it is lost",
          msg.answers[-1][0], "خطا")

    print("\n--- an admin's chat is left alone ---")
    persistent_menu.resolve_admin_scope = AsyncMock(return_value={"role": "admin"})
    called.clear()
    await persistent_menu.on_menu_tap(FakeMessage("🛒 خرید اکانت جدید"), state=AsyncMock(), bot=object())
    check("the bar does not hijack an admin", called, {})
    persistent_menu.resolve_admin_scope = AsyncMock(return_value=None)

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
check("...only to customers", "scope is None" in src, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("منوی ثابت پایین چت کار می‌کند و همان کد دکمه‌های داخل پیام را اجرا می‌کند")
