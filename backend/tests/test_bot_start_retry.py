"""A bot whose first contact with Telegram fails on the network keeps trying
(bounded backoff, visible in its status) instead of staying off until a
manual restart - and never starts polling once it has been stopped.

Run:  python3 backend/tests/test_bot_start_retry.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError  # noqa: E402
from aiogram.methods import GetMe  # noqa: E402

from app.telegram_bot import runner  # noqa: E402

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class Me:
    username = "shop_bot"


class FakeBot:
    def __init__(self, script):
        self.script = list(script)    # "net" | "auth" | "ok" | callable
        self.calls = 0

    async def get_me(self):
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        if callable(step):
            step = step()
        if step == "net":
            raise TelegramNetworkError(method=GetMe(), message="Connection reset by peer")
        if step == "auth":
            raise TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
        return Me()


waits: list[float] = []


async def fast_sleep(seconds, stop_event):
    waits.append(seconds)
    return not stop_event.is_set()


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


with patch.object(runner, "_sleep_unless_stopped", fast_sleep):
    print("--- a network failure at start is retried, with a growing, bounded wait ---")
    inst = runner._Instance()
    bot = FakeBot(["net", "net", "net", "ok"])
    me = run(runner._connect_with_retry(inst, bot, threading.Event(), 20))
    check("connected after the network came back", (me.username, bot.calls), ("shop_bot", 4))
    check("waits grow along the schedule", waits, list(runner.START_RETRY_DELAYS[:3]))
    check("the retry note is cleared once connected", "retry_attempt" in inst.status, False)

    print("--- the wait is capped and the attempts continue ---")
    waits.clear()
    inst = runner._Instance()
    bot = FakeBot(["net"] * 12 + ["ok"])
    run(runner._connect_with_retry(inst, bot, threading.Event(), 20))
    check("never longer than the last step", max(waits), runner.START_RETRY_DELAYS[-1])
    check("all twelve failures were retried", bot.calls, 13)

    print("--- the failure is visible while it lasts ---")
    waits.clear()
    inst = runner._Instance()
    seen = []
    stop = threading.Event()

    def look():
        seen.append(dict(inst.status))
        return "ok"
    bot = FakeBot(["net", look])
    run(runner._connect_with_retry(inst, bot, stop, 20))
    check("status says it is retrying, not that the token is bad",
          (seen[0]["running"], "تلاش دوباره 1" in seen[0]["last_error"], seen[0]["retry_attempt"]), (False, True, 1))

    print("--- a rejected token is not retried ---")
    waits.clear()
    inst = runner._Instance()
    bot = FakeBot(["auth", "ok"])
    check("gives up at once", run(runner._connect_with_retry(inst, bot, threading.Event(), 20)), None)
    check("...after a single call, with no wait", (bot.calls, waits), (1, []))
    check("...and says the token is invalid", "توکن ربات نامعتبر است" in inst.status["last_error"], True)

    print("--- a stop (shutdown, restart, token change) ends the retrying ---")
    waits.clear()
    inst = runner._Instance()
    stop = threading.Event()

    def stop_now():
        stop.set()
        return "net"
    bot = FakeBot([stop_now, "ok"])
    check("stopped during the wait: no further attempt", run(runner._connect_with_retry(inst, bot, stop, 20)), None)
    check("...exactly one call", bot.calls, 1)

    inst = runner._Instance()
    stop = threading.Event()

    def answer_after_stop():
        stop.set()
        return "ok"
    bot = FakeBot([answer_after_stop])
    check("stopped while the answer was on its way: does not start (no second poller)",
          run(runner._connect_with_retry(inst, bot, stop, 20)), None)

print("--- the real wait honours the stop event ---")
stop = threading.Event()
stop.set()
check("an already-stopped bot does not wait", run(runner._sleep_unless_stopped(60, stop)), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
