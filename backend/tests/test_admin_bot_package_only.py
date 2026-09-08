"""telegram_bot/handlers/admin_users.py's user-creation and renewal flows -
package selection only, no manual quota (GB) / duration (days) entry.

Run:  python3 backend/tests/test_admin_bot_package_only.py

Feature reported 2026-09-08: inside the Telegram ADMIN bot's "➕ ساخت کاربر"
and "♻️ تمدید سرویس" flows, the admin used to be asked to TYPE a quota (GB)
and a duration (days) as free text. Both prompts are removed - the admin now
always picks one of the panel's own packages (states.AdminCreateUserStates.
picking_package / AdminRenewStates.picking_package, callbacks.
AdminCreatePkgCB / AdminRenewPkgCB), and the package's own quota_gb/
duration_days are what get applied - never a number the admin typed.

This drives the real aiogram handler functions directly (they are plain
coroutines - no live Bot/Dispatcher needed) against a real FSMContext
(aiogram's own MemoryStorage), with only the outbound panel_bridge.api calls
faked, and with Telegram objects (CallbackQuery/Message) replaced by minimal
stand-ins carrying just the attributes/methods the handlers actually touch.
"""
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


from aiogram.fsm.context import FSMContext  # noqa: E402
from aiogram.fsm.storage.memory import MemoryStorage  # noqa: E402
from aiogram.fsm.storage.base import StorageKey  # noqa: E402

from app.telegram_bot.handlers import admin_users  # noqa: E402
from app.telegram_bot import states, callbacks  # noqa: E402


def make_state() -> FSMContext:
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))


class FakeMessage:
    def __init__(self):
        self.edits: list[tuple[str, object]] = []
        self.answers: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None):
        self.edits.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.answers.append((text, reply_markup))


class FakeCall:
    def __init__(self):
        self.message = FakeMessage()
        self.from_user = type("U", (), {"id": 999})()
        self.answered: list[tuple] = []

    async def answer(self, *a, **kw):
        self.answered.append((a, kw))


PACKAGES = [
    {"id": 5, "name": "۲۰ گیگ ۱ ماه", "quota_gb": 20, "duration_days": 30, "price": 100000},
    {"id": 6, "name": "نامحدود", "quota_gb": 0, "duration_days": 30, "price": 200000},
]


async def run():
    acting_scope = {"owner_admin_id": None}

    # ------------------------------------------------------------ create user
    print("--- picking a protocol shows a PACKAGE picker, never asks for GB/days ---")
    state = make_state()
    await state.update_data(new_username="alice", node_id=1, node_name="node1", nodes={1: {"id": 1, "type": "wireguard", "name": "node1"}})
    await state.set_state(states.AdminCreateUserStates.picking_protocol)
    call = FakeCall()
    admin_users.api.list_packages = AsyncMock(return_value=PACKAGES)
    cb = callbacks.ProtocolCB(protocol="wireguard")
    await admin_users.admin_pick_protocol(call, cb, state, acting_scope)
    check("moved to picking_package (not waiting_quota)", await state.get_state(), states.AdminCreateUserStates.picking_package.state)
    check("the prompt text mentions پکیج, not GB", "پکیج" in call.message.edits[-1][0], True)
    check("...and does NOT ask for حجم (GB)", "حجم" in call.message.edits[-1][0], False)

    print("\n--- picking the package creates the user with THAT package's own quota/duration ---")
    admin_users.api.create_user = AsyncMock(return_value={"username": "alice", "status": "active", "connections": []})
    pkg_cb = callbacks.AdminCreatePkgCB(package_id=5)
    fake_bot = object()
    await admin_users.admin_create_pick_package(call, pkg_cb, state, acting_scope, fake_bot)
    _, kwargs = admin_users.api.create_user.call_args
    check("quota_gb comes from the package, not any typed value", kwargs["quota_gb"], 20)
    check("expire_days comes from the package's duration_days", kwargs["expire_days"], 30)
    check("package_id is passed through", kwargs["package_id"], 5)
    check("state is cleared after success", await state.get_state(), None)

    print("\n--- an UNLIMITED package (quota_gb=0) still creates with 0, not None or a stale value ---")
    state2 = make_state()
    await state2.update_data(new_username="bob", node_id=1, protocol="wireguard")
    await state2.set_state(states.AdminCreateUserStates.picking_package)
    call2 = FakeCall()
    await admin_users.admin_create_pick_package(call2, callbacks.AdminCreatePkgCB(package_id=6), state2, acting_scope, fake_bot)
    _, kwargs2 = admin_users.api.create_user.call_args
    check("unlimited package -> quota_gb 0", kwargs2["quota_gb"], 0)
    check("unlimited package -> expire_days still comes from duration_days", kwargs2["expire_days"], 30)

    # ------------------------------------------------------------------ renew
    print("\n--- asking to renew shows a PACKAGE picker, never the old '<gb> <days>' prompt ---")
    admin_users.api.list_purchases = AsyncMock(return_value=[{"id": 42}])
    state3 = make_state()
    call3 = FakeCall()
    await admin_users.cb_user_renew_ask(call3, callbacks.AdminUserCB(action="renew", username="carol"), state3, acting_scope)
    check("moved to AdminRenewStates.picking_package", await state3.get_state(), states.AdminRenewStates.picking_package.state)
    check("purchase_id carried into state", (await state3.get_data())["purchase_id"], 42)
    check("prompt mentions پکیج", "پکیج" in call3.message.edits[-1][0], True)
    check("prompt does NOT ask to type GB/days", "GB" in call3.message.edits[-1][0], False)

    print("\n--- picking a renewal package renews with THAT package's quota/duration + package_id ---")
    admin_users.api.renew_service = AsyncMock(return_value={"username": "carol", "status": "active"})
    admin_users.api.get_user = AsyncMock(return_value={"username": "carol", "status": "active", "balance": 0, "connections": []})
    await admin_users.admin_renew_pick_package(call3, callbacks.AdminRenewPkgCB(package_id=5), state3, acting_scope)
    args, kwargs3 = admin_users.api.renew_service.call_args
    check("renews the SAME purchase carried in state", args[1], 42)
    check("add_gb comes from the package", kwargs3["add_gb"], 20)
    check("add_days comes from the package", kwargs3["add_days"], 30)
    check("package_id is passed through (so it is billed/recorded as a real sale)", kwargs3["package_id"], 5)
    check("state cleared after a successful renewal", await state3.get_state(), None)

    print("\n--- no packages configured at all: refused with a clear message, no crash ---")
    state4 = make_state()
    await state4.update_data(new_username="dave", node_id=1, node_name="node1", nodes={1: {"id": 1, "type": "wireguard", "name": "node1"}})
    await state4.set_state(states.AdminCreateUserStates.picking_protocol)
    call4 = FakeCall()
    admin_users.api.list_packages = AsyncMock(return_value=[])
    await admin_users.admin_pick_protocol(call4, callbacks.ProtocolCB(protocol="wireguard"), state4, acting_scope)
    check("state cleared rather than left dangling", await state4.get_state(), None)
    check("admin is told to make a package first", "پکیج" in call4.message.edits[-1][0], True)


asyncio.run(run())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
