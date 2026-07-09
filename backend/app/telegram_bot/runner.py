"""Runs the built-in Telegram bot on its own background thread + private
asyncio event loop - the same pattern app/services/radius_server.py uses
for the RADIUS server - so it doesn't share/block FastAPI's own event loop
and can be cleanly stopped/restarted whenever the admin changes the bot
settings from the web UI. No process restart, no .env file, no SSH."""
from __future__ import annotations

import asyncio
import logging
import threading

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, FSInputFile

from .config import config
from .handlers import build_router
from . import storage as bot_storage

logger = logging.getLogger("telegram_bot")

MAINTENANCE_TEXT = "🔧 ربات موقتاً در دسترس نیست، لطفاً بعداً دوباره تلاش کنید."


class MaintenanceModeMiddleware(BaseMiddleware):
    """Outer middleware (registered on both dp.message and
    dp.callback_query in _main below, so it runs before ANY router/filter -
    catching /start too) that blocks every customer-facing interaction
    while config.customer_bot_enabled is False, answering with
    MAINTENANCE_TEXT instead of letting the update reach the normal
    handlers. Admins (global admin_ids AND linked group-admins - see
    admin_scope.resolve_admin_scope) are never affected, so they can keep
    managing the panel/customers and re-enable it from the Settings page
    while customers are locked out."""

    async def __call__(self, handler, event, data):
        if config.customer_bot_enabled:
            return await handler(event, data)

        user = data.get("event_from_user")
        if user and not config.is_admin(user.id):
            from .admin_scope import resolve_admin_scope

            scope = await resolve_admin_scope(user.id)
            if not scope:
                try:
                    if isinstance(event, CallbackQuery):
                        # A lightweight alert popup instead of yet another
                        # chat message for every button tap.
                        await event.answer(MAINTENANCE_TEXT, show_alert=True)
                    else:
                        await event.answer(MAINTENANCE_TEXT)
                except Exception:
                    pass
                return None
        return await handler(event, data)

# Populates Telegram's native "Menu" button (the slash-command popup next
# to the message box) - shortcuts for the same actions already reachable
# via the inline keyboard buttons (see handlers/*.py's matching Command()
# handlers). Customer commands are visible to everyone; admins additionally
# get ADMIN_EXTRA_COMMANDS via a per-chat scope (see _set_bot_commands).
CUSTOMER_COMMANDS = [
    BotCommand(command="start", description="🔑 شروع"),
    BotCommand(command="account", description="👤 اکانت من"),
    BotCommand(command="buy", description="🛒 خرید اکانت جدید"),
    BotCommand(command="topup", description="💰 افزایش اعتبار"),
    BotCommand(command="tutorials", description="📚 آموزش"),
    BotCommand(command="link", description="🔗 وصل کردن حساب قبلی"),
    BotCommand(command="help", description="راهنما"),
]

ADMIN_EXTRA_COMMANDS = [
    BotCommand(command="newuser", description="➕ ساخت کاربر"),
    BotCommand(command="users", description="📋 لیست کاربران"),
    BotCommand(command="pending", description="📥 درخواست‌های در انتظار"),
    BotCommand(command="broadcast", description="📢 پیام همگانی"),
]


async def _set_bot_commands(bot: Bot, admin_ids: set) -> None:
    """Best-effort - a failure here (e.g. transient Telegram API hiccup)
    shouldn't stop the bot from starting, it would just mean the "Menu"
    button is empty/stale until the next restart.

    Note for diagnosing "admin sees the menu, customers don't" reports:
    that's almost always Telegram's CLIENT caching the command list from
    before it had any/the old commands - the server-side call below covers
    every customer via the default scope in one shot (there's no per-user
    step needed), but an already-open chat on a customer's phone/desktop
    often won't refresh its Menu button until that chat is closed and
    reopened (or the app is restarted) - re-sending /start does not force
    a refresh by itself. The log lines below confirm whether the default
    (customer) call actually succeeded server-side, to rule that out."""
    try:
        await bot.set_my_commands(CUSTOMER_COMMANDS, scope=BotCommandScopeDefault())
        logger.info("bot commands menu set for default scope (all customers): %d commands", len(CUSTOMER_COMMANDS))
    except Exception:
        logger.exception("failed to set default bot commands menu - customers will see an empty/stale Menu button")
    for admin_id in admin_ids:
        try:
            await bot.set_my_commands(
                CUSTOMER_COMMANDS + ADMIN_EXTRA_COMMANDS,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
            logger.info("bot commands menu set for admin chat %s", admin_id)
        except Exception:
            # Expected/harmless if this admin id has never opened a chat
            # with the bot yet - Telegram rejects scope=chat for a chat it
            # doesn't know about. Still logged (not silently swallowed) so
            # a genuine failure for an active admin is visible in the logs.
            logger.warning("failed to set bot commands menu for admin chat %s", admin_id, exc_info=True)


_lock = threading.Lock()
_thread: threading.Thread | None = None
_stop_event: threading.Event | None = None
_status: dict = {"running": False, "last_error": None, "bot_username": None}

# Set while the bot is actually polling for updates - currently only read
# by get_status() for the Settings page's "فعال / غیرفعال" badge.
# send_message_sync/send_document_sync below deliberately do NOT use these
# (see their docstrings) so outbound sends keep working even when nothing
# is polling in-process (e.g. the interactive bot is running on a remote
# server instead - see telegram_bot/remote_bridge.py).
_bot = None
_loop: asyncio.AbstractEventLoop | None = None


def get_status() -> dict:
    return dict(_status)


def _lookup_bot_token() -> str | None:
    """Reads the bot token straight from the BotSettings DB row - a local
    import to avoid a hard circular-import dependency between this module
    and the main app package at load time."""
    from ..database import SessionLocal
    from .. import models

    db = SessionLocal()
    try:
        row = db.get(models.BotSettings, 1)
        return row.bot_token if row and row.bot_token else None
    finally:
        db.close()


def send_message_sync(chat_id: int, text: str, timeout: float = 10.0) -> bool:
    """Thread-safe, best-effort message send for callers running OUTSIDE
    the bot's own event loop/thread (aiogram's Bot is not thread-safe to
    call directly) - e.g. the daily quota/expiry reminder job and the 4x/day
    backup job, both running synchronously on APScheduler's own thread.

    Deliberately does NOT reuse the actively-polling bot's _bot/_loop -
    spins up its own short-lived Bot(token) instead. This matters once the
    admin can move the INTERACTIVE bot (the one doing getUpdates polling)
    to a remote server: Telegram only allows one poller per token, but
    sending messages via the Bot API has no such restriction, so this
    server can always push notifications/backups through the same token
    regardless of where the polling bot currently lives. Returns False
    (never raises) if no token is configured or the send failed - e.g. the
    customer blocked the bot, which is expected often enough that it
    shouldn't be treated as an error by callers."""
    token = _lookup_bot_token()
    if not token:
        return False

    async def _send():
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await asyncio.wait_for(bot.send_message(chat_id, text), timeout=timeout)
        finally:
            await bot.session.close()

    try:
        asyncio.run(_send())
        return True
    except Exception:
        return False


def send_document_sync(chat_id: int, file_path: str, caption: str = "", timeout: float = 30.0) -> bool:
    """Same idea as send_message_sync but for a file on disk - used by
    services/backup.py to deliver database backups to the bot admins.
    Longer default timeout since backup files can be a few MB."""
    token = _lookup_bot_token()
    if not token:
        return False

    async def _send():
        bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        try:
            await asyncio.wait_for(
                bot.send_document(chat_id, FSInputFile(file_path), caption=caption or None), timeout=timeout
            )
        finally:
            await bot.session.close()

    try:
        asyncio.run(_send())
        return True
    except Exception:
        return False


def _run_loop(
    token: str, admin_ids: set, approval_chat_ids: set, stop_event: threading.Event,
    customer_bot_enabled: bool = True,
) -> None:
    global _loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _loop = loop
    try:
        loop.run_until_complete(_main(token, admin_ids, approval_chat_ids, stop_event, customer_bot_enabled))
    finally:
        _loop = None
        loop.close()


async def _main(
    token: str, admin_ids: set, approval_chat_ids: set, stop_event: threading.Event,
    customer_bot_enabled: bool = True,
) -> None:
    global _bot
    config.configure(token, admin_ids, approval_chat_ids, customer_bot_enabled)
    bot_storage.init_db()

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception as exc:
        _status.update(running=False, last_error=f"توکن نامعتبر است یا تلگرام در دسترس نیست: {exc}")
        logger.exception("failed to start telegram bot")
        await bot.session.close()
        return

    _bot = bot
    _status.update(running=True, last_error=None, bot_username=me.username)
    logger.info("Telegram bot started: @%s", me.username)
    await _set_bot_commands(bot, admin_ids)

    dp = Dispatcher(storage=MemoryStorage())
    maintenance_mw = MaintenanceModeMiddleware()
    dp.message.outer_middleware(maintenance_mw)
    dp.callback_query.outer_middleware(maintenance_mw)
    dp.include_router(build_router())

    polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
    try:
        while not stop_event.is_set():
            if polling_task.done():
                # polling died on its own (network issue etc.) - surface it
                exc = polling_task.exception()
                if exc:
                    _status.update(running=False, last_error=str(exc))
                    logger.exception("telegram bot polling stopped unexpectedly", exc_info=exc)
                break
            await asyncio.sleep(0.5)
    finally:
        if not polling_task.done():
            polling_task.cancel()
            try:
                await polling_task
            except (asyncio.CancelledError, Exception):
                pass
        await bot.session.close()
        _bot = None
        _status.update(running=False)
        logger.info("Telegram bot stopped")


def start_bot(token: str, admin_ids: set, approval_chat_ids: set, customer_bot_enabled: bool = True) -> None:
    """Starts the bot on a background thread. No-op if already running -
    call restart_bot() to apply changed settings to a running bot."""
    global _thread, _stop_event
    with _lock:
        if _thread and _thread.is_alive():
            return
        if not token or not admin_ids:
            _status.update(running=False, last_error="توکن ربات یا آیدی عددی ادمین تنظیم نشده است")
            return
        _stop_event = threading.Event()
        _thread = threading.Thread(
            target=_run_loop,
            args=(token, set(admin_ids), set(approval_chat_ids), _stop_event, customer_bot_enabled),
            name="telegram-bot",
            daemon=True,
        )
        _thread.start()


def stop_bot(timeout: float = 10.0) -> None:
    global _thread
    with _lock:
        if _stop_event:
            _stop_event.set()
        thread = _thread
        _thread = None
    if thread:
        thread.join(timeout=timeout)
    _status.update(running=False)


def restart_bot(
    token: str, admin_ids: set, approval_chat_ids: set, enabled: bool, customer_bot_enabled: bool = True,
) -> None:
    """Stops whatever is currently running (if anything) and starts fresh
    with the new settings - called every time the admin saves the bot
    settings page, so changes take effect immediately without a container
    restart."""
    stop_bot()
    if enabled and token and admin_ids:
        start_bot(token, admin_ids, approval_chat_ids, customer_bot_enabled)
    else:
        _status.update(running=False, last_error=None if enabled else None)
