"""Regression checks for customer tariffs and reseller applications."""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.telegram_bot import panel_bridge  # noqa: E402
from app.telegram_bot.config import config  # noqa: E402
from app.telegram_bot.handlers import customer_engagement as engagement  # noqa: E402
from app.telegram_bot.states import CustomerAgentRequestStates  # noqa: E402


failures = []


def check(name, condition):
    if condition:
        print(f"PASS {name}")
    else:
        failures.append(name)
        print(f"FAIL {name}")


class FakeMessage:
    def __init__(self, text="", user=None):
        self.text = text
        self.from_user = user or SimpleNamespace(id=123, full_name="Test User", username="test")
        self.sent = []

    async def answer(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def edit_text(self, text, **kwargs):
        self.sent.append((text, kwargs))


class FakeCall:
    def __init__(self):
        self.message = FakeMessage()
        self.answers = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))


async def run():
    old_disabled = panel_bridge.get_customer_menu_disabled_items_cached
    old_packages = panel_bridge.api.list_packages
    old_targets = config.approval_chat_ids
    old_admins = config.admin_ids
    panel_bridge.get_customer_menu_disabled_items_cached = AsyncMock(return_value=[])
    panel_bridge.api.list_packages = AsyncMock(return_value=[{
        "name": "پکیج تست", "quota_gb": 20, "duration_days": 30, "price": 100000,
    }])

    call = FakeCall()
    tariff_state = AsyncMock()
    await engagement.cb_prices(call, tariff_state)
    check("tariff view renders the live package and price",
          "پکیج تست" in call.message.sent[0][0] and "۱۰۰,۰۰۰ تومان" in call.message.sent[0][0])
    check("opening tariffs exits any unfinished flow", tariff_state.clear.await_count == 1)

    state = AsyncMock()
    call = FakeCall()
    await engagement.cb_agent_request(call, state)
    check("agent request prompts for a message", "درخواست نمایندگی" in call.message.sent[0][0])
    check("agent request enters its dedicated FSM state",
          state.set_state.await_args.args[0] == CustomerAgentRequestStates.waiting_message)

    config.admin_ids = {9001, 9002}
    config.approval_chat_ids = set()
    bot = SimpleNamespace(send_message=AsyncMock())
    message = FakeMessage("درخواست من <آزمایشی>")
    state.clear = AsyncMock()
    await engagement.receive_agent_request(message, state, bot)
    check("request is sent to configured admin targets", bot.send_message.await_count == 2)
    sent_body = bot.send_message.await_args_list[0].args[1]
    check("customer-provided markup is escaped", "&lt;آزمایشی&gt;" in sent_body and "<آزمایشی>" not in sent_body)
    check("successful request clears the state", state.clear.await_count == 1)

    config.admin_ids = set()
    config.approval_chat_ids = set()
    bot.send_message.reset_mock()
    message = FakeMessage("درخواست")
    state.clear.reset_mock()
    await engagement.receive_agent_request(message, state, bot)
    check("missing destination does not silently accept the request",
          "مقصدی" in message.sent[0][0] and bot.send_message.await_count == 0)

    panel_bridge.get_customer_menu_disabled_items_cached = old_disabled
    panel_bridge.api.list_packages = old_packages
    config.admin_ids = old_admins
    config.approval_chat_ids = old_targets


asyncio.run(run())
if failures:
    print(f"{len(failures)} failure(s): {failures}")
    raise SystemExit(1)
print("All customer engagement checks passed")
