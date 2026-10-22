"""Telling the admins that a receipt was approved AUTOMATICALLY.

The approval itself (provisioning, credit, rewards) has already finished
when anything here runs, and nothing here can repeat it: a notice is a row
in the bot's own storage (storage.approval_notices) saying "send this text
and this receipt photo to that chat". Delivery is retried with a bounded
backoff by the same bot that received the receipt - a Telegram file id is
valid only for that bot.

Delivery is at-least-once, not exactly-once: if Telegram accepts a message
but the answer is lost (a timeout), the retry sends it again. The Bot API
offers no way to ask whether a message was delivered, so a duplicate notice
is possible in that case; a missing one is not.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from typing import Optional

from aiogram.exceptions import (
    TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter, TelegramUnauthorizedError,
)

from aiogram.types import BufferedInputFile

from . import storage
from .config import config

logger = logging.getLogger("approval_notices")

# seconds after attempt 1, 2, ...; after the last one the notice is abandoned
# (still listed by /notices and retryable by hand).
BACKOFF = (60, 300, 900, 1800, 3600)
CAPTION_LIMIT = 1024
WORKER_INTERVAL = 30


def bot_id(token: Optional[str] = None) -> str:
    """The bot's numeric id - the part of the token before ':' (public)."""
    return ((token if token is not None else config.bot_token) or "").split(":")[0]


def compose(pending: dict, owner_label: Optional[str], result: str, reason: str) -> str:
    from .handlers.admin_pending import _pending_summary
    return ("🤖 <b>خودکار تأیید شد</b> — نیازی به تأیید شما نیست\n\n"
            + _pending_summary(pending, owner_label)
            + f"\n\nنتیجه: {result}\nدلیل تأیید خودکار: {reason}")


async def queue_auto_approved(pending: dict, result: str, reason: str, photo: Optional[bytes] = None) -> None:
    """Record who must be told. Never raises: the approval is already done.
    photo: the receipt image when it has no Telegram file id (Mini App)."""
    from .handlers.admin_pending import _owner_label
    from .handlers.customer_common import _notify_targets
    try:
        label = await _owner_label(pending.get("owner_admin_id"))
    except Exception:
        label = None
    text = compose(pending, label, result, reason)
    try:
        targets = await _notify_targets(pending)
    except Exception:
        logger.exception("auto-approval notice: recipients of request %s not resolved yet", pending.get("id"))
        targets = None
    try:
        if targets is None:
            storage.queue_notices(pending["id"], bot_id(), [0], text, photo)      # resolve on retry
        elif not targets:
            storage.queue_notices(pending["id"], bot_id(), [0], text, photo)
            logger.warning("auto-approval notice: request %s has no admin to notify", pending.get("id"))
        else:
            storage.queue_notices(pending["id"], bot_id(), sorted(targets), text, photo)
    except Exception:
        logger.exception("auto-approval notice for request %s could not be recorded", pending.get("id"))


def _next(attempts_done: int, retry_after: Optional[int] = None) -> Optional[str]:
    """When to try again after `attempts_done` failures; None = give up."""
    if attempts_done > len(BACKOFF):
        return None
    delay = max(BACKOFF[attempts_done - 1], int(retry_after or 0))
    return (dt.datetime.utcnow() + dt.timedelta(seconds=delay)).replace(microsecond=0).isoformat()


async def _resolve(notice: dict) -> None:
    """A chat_id 0 row: find the recipients now and queue them."""
    from .handlers.customer_common import _notify_targets
    pending = storage.get_pending(notice["request_id"])
    targets = await _notify_targets(pending) if pending else set()
    if not targets:
        when = _next(notice["attempts"] + 1)
        storage.mark_notice(notice["id"], "failed" if when else "gave_up", error="no_recipients", next_attempt_at=when)
        return
    storage.queue_notices(notice["request_id"], notice["bot_id"], sorted(targets), notice["text"], notice.get("photo"))
    storage.close_resolver_notice(notice["id"])


async def _send(bot, notice: dict) -> None:
    pending = storage.get_pending(notice["request_id"]) or {}
    photo = pending.get("receipt_file_id")
    if not photo and notice.get("photo"):
        photo = BufferedInputFile(notice["photo"], filename=f"receipt-{notice['request_id']}.jpg")
    text = notice["text"]
    if photo and len(text) <= CAPTION_LIMIT:
        await bot.send_photo(notice["chat_id"], photo, caption=text)
    elif photo:
        await bot.send_photo(notice["chat_id"], photo, caption=text[:CAPTION_LIMIT - 1] + "…")
        await bot.send_message(notice["chat_id"], text)
    else:
        await bot.send_message(notice["chat_id"], text)


async def deliver_due(bot, request_id: Optional[int] = None) -> int:
    """Send what is due for THIS bot. Returns how many were sent."""
    sent = 0
    for notice in storage.due_notices(bot_id(), request_id=request_id):
        try:
            if notice["chat_id"] == 0:
                await _resolve(notice)
                continue
            await _send(bot, notice)
        except TelegramRetryAfter as exc:
            when = _next(notice["attempts"] + 1, exc.retry_after)
            storage.mark_notice(notice["id"], "failed" if when else "gave_up", error="flood_control", next_attempt_at=when)
        except (TelegramForbiddenError, TelegramUnauthorizedError, TelegramNotFound, TelegramBadRequest) as exc:
            # Blocked bot, unknown chat, unusable file: retrying cannot help.
            storage.mark_notice(notice["id"], "gave_up", error=f"{type(exc).__name__}: {exc}"[:300])
            logger.warning("auto-approval notice %s to %s abandoned: %s", notice["id"], notice["chat_id"], type(exc).__name__)
        except Exception as exc:  # network, timeout, server error: delivery unknown
            when = _next(notice["attempts"] + 1)
            storage.mark_notice(notice["id"], "failed" if when else "gave_up",
                                error=f"{type(exc).__name__}: {exc}"[:300], next_attempt_at=when)
            logger.warning("auto-approval notice %s to %s failed (%s); %s", notice["id"], notice["chat_id"],
                           type(exc).__name__, "retry scheduled" if when else "giving up")
        else:
            storage.mark_notice(notice["id"], "sent")
            sent += 1
    return sent


async def run_worker(bot, stop_event) -> None:
    """Background retry loop of one bot instance; stops with the bot."""
    while not stop_event.is_set():
        try:
            await deliver_due(bot)
        except Exception:
            logger.exception("auto-approval notice worker pass failed")
        for _ in range(WORKER_INTERVAL * 2):
            if stop_event.is_set():
                return
            await asyncio.sleep(0.5)
