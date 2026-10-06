"""Customer channel/terms onboarding gate. Run with tests/run_all.py."""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from unittest.mock import AsyncMock, Mock
from fastapi import HTTPException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import models
from app.database import Base
from app.routers import bot_onboarding
from app.telegram_bot import admin_scope, panel_bridge, runner
from aiogram.types import CallbackQuery
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.bot_auth import BotPrincipal, KeyType

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}: got {got!r}, expected {expected!r}")


class FakeMessage:
    def __init__(self, text="/start", user_id=123):
        self.text = text
        self.from_user = type("User", (), {"id": user_id})()
        self.answers = []

    async def answer(self, text, **kwargs):
        self.answers.append((text, kwargs))
        return self


class FakeCallback:
    def __new__(cls, data, user_id=123):
        callback = Mock(spec=CallbackQuery)
        callback.data = data
        callback.from_user = type("User", (), {"id": user_id})()
        callback.message = FakeMessage(user_id=user_id)
        callback.answer = AsyncMock()
        return callback


async def run():
    print("--- durable terms acceptance API ---")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        principal = BotPrincipal.internal(None)
        db.add(models.BotSettings(
            id=1, required_channel_id="@sample", required_channel_url="https://t.me/sample",
            customer_terms_text="terms one",
        ))
        db.commit()
        config = bot_onboarding.get_customer_onboarding_config(db, principal=principal)
        digest_one = hashlib.sha256(b"terms one").hexdigest()
        digest_two = hashlib.sha256(b"terms two").hexdigest()
        check("onboarding API returns admin configuration", config["required_channel_id"], "@sample")
        check("acceptance starts absent", bot_onboarding.has_customer_accepted_terms(
            123, digest_one, db=db, principal=principal),
              {"accepted": False})
        payload = bot_onboarding.TermsAcceptanceIn(telegram_id=123, terms_digest=digest_one)
        bot_onboarding.accept_customer_terms(payload, db=db, principal=principal)
        bot_onboarding.accept_customer_terms(payload, db=db, principal=principal)  # duplicate tap is idempotent
        check("acceptance is durable and duplicate-safe",
              bot_onboarding.has_customer_accepted_terms(123, digest_one, db=db, principal=principal), {"accepted": True})
        check("a changed terms digest needs fresh acceptance",
              bot_onboarding.has_customer_accepted_terms(123, digest_two, db=db, principal=principal), {"accepted": False})
        external = BotPrincipal(
            key_id=42, key_type=KeyType.GLOBAL_INTEGRATION, owner_admin_id=None,
            capabilities=frozenset(), label="test integration",
        )
        try:
            bot_onboarding.get_customer_onboarding_config(db, principal=external)
            denied = False
        except HTTPException as exc:
            denied = exc.status_code == 403
        check("third-party integration cannot inspect or spoof interactive onboarding", denied, True)
    engine.dispose()

    old_scope = admin_scope.resolve_admin_scope
    old_api = {
        name: getattr(panel_bridge.api, name)
        for name in ("get_customer_onboarding_config", "customer_terms_accepted", "accept_customer_terms")
    }
    old_start = None
    try:
        admin_scope.resolve_admin_scope = AsyncMock(return_value=None)
        panel_bridge.api.get_customer_onboarding_config = AsyncMock(return_value={
            "required_channel_id": "@sample", "required_channel_url": "https://t.me/sample",
            "customer_terms_text": "",
        })
        panel_bridge.api.customer_terms_accepted = AsyncMock(return_value=False)
        panel_bridge.api.accept_customer_terms = AsyncMock()
        gate = runner.CustomerOnboardingMiddleware()
        gate._member_cache.clear()
        handler = AsyncMock(return_value="passed")
        bot = type("Bot", (), {"get_chat_member": AsyncMock(return_value=type("Member", (), {"status": "left"})())})()

        print("--- required channel membership ---")
        msg = FakeMessage()
        result = await gate(handler, msg, {"bot": bot, "event_from_user": msg.from_user})
        markup = msg.answers[-1][1]["reply_markup"]
        check("non-member is stopped before customer handlers", result, None)
        check("join prompt provides link and explicit recheck", [b.url or b.callback_data for row in markup.inline_keyboard for b in row],
              ["https://t.me/sample", gate.CHECK_CALLBACK])
        check("membership check is cached for normal updates", bot.get_chat_member.await_count, 1)

        # The explicit recheck must bypass a cached negative after joining.
        bot.get_chat_member.return_value = type("Member", (), {"status": "member"})()
        panel_bridge.api.get_customer_onboarding_config.return_value = {
            "required_channel_id": "@sample", "required_channel_url": "https://t.me/sample",
            "customer_terms_text": "",
        }
        from app.telegram_bot.handlers import start
        old_start = start.send_start_screen
        start.send_start_screen = AsyncMock()
        cb = FakeCallback(gate.CHECK_CALLBACK)
        result = await gate(handler, cb, {"bot": bot, "event_from_user": cb.from_user})
        check("manual membership recheck bypasses cached negative", bot.get_chat_member.await_count, 2)
        check("successful membership check shows the start screen", start.send_start_screen.await_count, 1)
        check("recheck callback is consumed, not sent to normal routes", result, None)

        print("--- terms acceptance is tied to the exact text ---")
        terms = "شرایط نمونه"
        digest = hashlib.sha256(terms.encode()).hexdigest()
        panel_bridge.api.get_customer_onboarding_config.return_value = {
            "required_channel_id": "", "required_channel_url": "", "customer_terms_text": terms,
        }
        panel_bridge.api.customer_terms_accepted.return_value = False
        msg = FakeMessage()
        await gate(handler, msg, {"bot": bot, "event_from_user": msg.from_user})
        terms_markup = msg.answers[-1][1]["reply_markup"]
        accept_data = terms_markup.inline_keyboard[0][0].callback_data
        check("terms shown as plain text with a digest-bound acceptance button",
              (msg.answers[-1][0].endswith(terms), accept_data),
              (True, gate.ACCEPT_CALLBACK_PREFIX + digest[:gate.ACCEPT_DIGEST_HEX_LENGTH]))
        check("acceptance callback stays within Telegram's 64-byte limit", len(accept_data.encode()), 64)

        cb = FakeCallback(gate.ACCEPT_CALLBACK_PREFIX + digest[:gate.ACCEPT_DIGEST_HEX_LENGTH])
        await gate(handler, cb, {"bot": bot, "event_from_user": cb.from_user})
        check("acceptance is persisted with the current digest",
              panel_bridge.api.accept_customer_terms.await_args.args, (123, digest))
        check("acceptance returns the user to the start menu", start.send_start_screen.await_count, 2)
        check("terms acceptance callback is consumed", handler.await_count, 0)
    finally:
        admin_scope.resolve_admin_scope = old_scope
        for name, value in old_api.items():
            setattr(panel_bridge.api, name, value)
        if old_start is not None:
            start.send_start_screen = old_start


asyncio.run(run())
if failures:
    print("\nFAILED: " + ", ".join(failures))
    raise SystemExit(1)
print("\nCustomer onboarding gates passed")
