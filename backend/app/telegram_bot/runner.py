"""Runs the built-in Telegram bot on its own background thread + private
asyncio event loop - the same pattern app/services/radius_server.py uses
for the RADIUS server - so it doesn't share/block FastAPI's own event loop
and can be cleanly stopped/restarted whenever the admin changes the bot
settings from the web UI. No process restart, no .env file, no SSH.

Multi-bot note (3-tier hierarchy feature): this module now runs a
REGISTRY of bot instances, not just one. Every function below takes an
optional `instance_key` - omitted (the default, `_MAIN`), it behaves
exactly as it always did (the single shared/global bot, backed by the
BotSettings DB row). Pass a level-2 Admin's own id instead to start/stop/
query THAT Admin's own dedicated bot (see AdminUser.own_bot_token) -
several instances run concurrently, each on its own thread with its own
Bot/Dispatcher/event loop, completely independent of one another. What
makes the REST of this file's code (and every handler in handlers/*.py)
not need to know or care which instance it's running under is
config.py's RuntimeConfig being a threading.local - see that module's
docstring."""
from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass, field

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, CallbackQuery, FSInputFile

from .config import config
from .handlers import build_router
from . import storage as bot_storage

logger = logging.getLogger("telegram_bot")

MAINTENANCE_TEXT = "🔧 ربات موقتاً در دسترس نیست، لطفاً بعداً دوباره تلاش کنید."

# Sentinel instance_key for the single shared/global bot - every existing
# caller (routers/telegram_bot_settings.py) that doesn't pass instance_key
# at all lands here, so pre-hierarchy behavior is completely unchanged.
_MAIN = "__main__"


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


@dataclass
class _Instance:
    """Everything runner.py used to keep as loose module-level globals
    (_thread/_stop_event/_bot/_loop/_status), now one bundle per running
    bot so any number of these can coexist - see the registry below."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    thread: threading.Thread | None = None
    stop_event: threading.Event | None = None
    status: dict = field(default_factory=lambda: {"running": False, "last_error": None, "bot_username": None})
    bot: Bot | None = None
    loop: asyncio.AbstractEventLoop | None = None


_registry_lock = threading.Lock()
_instances: dict[object, _Instance] = {}


def _get_instance(instance_key) -> _Instance:
    with _registry_lock:
        inst = _instances.get(instance_key)
        if inst is None:
            inst = _Instance()
            _instances[instance_key] = inst
        return inst


def get_status(instance_key=_MAIN) -> dict:
    return dict(_get_instance(instance_key).status)


def _lookup_bot_token() -> str | None:
    """Reads the SHARED/global bot's token straight from the BotSettings DB
    row - a local import to avoid a hard circular-import dependency between
    this module and the main app package at load time."""
    from ..database import SessionLocal
    from .. import models

    db = SessionLocal()
    try:
        row = db.get(models.BotSettings, 1)
        return row.bot_token if row and row.bot_token else None
    finally:
        db.close()


def _lookup_telegram_api_proxy_url() -> str | None:
    """See models.BotSettings.telegram_api_proxy_url's docstring - a single
    panel-wide setting (not per-instance) applied to EVERY bot this process
    starts: the shared bot AND every level-2 Admin/level-3 Seller's own
    bot. Read fresh every time a bot (re)starts, so saving a new proxy URL
    in Settings takes effect on that instance's next start/restart with no
    other code change needed. Empty/unset (the default) = None, meaning
    "connect directly" - exactly today's behavior for every existing
    deployment that doesn't need this."""
    from ..database import SessionLocal
    from .. import models

    db = SessionLocal()
    try:
        row = db.get(models.BotSettings, 1)
        url = (row.telegram_api_proxy_url or "").strip() if row else ""
        return url or None
    finally:
        db.close()


def _make_bot(token: str) -> Bot:
    """Single choke point for constructing an aiogram Bot - every bot
    instance (shared, every own-bot, and the one-off sends in
    send_message_sync/send_document_sync below) goes through this so the
    optional Telegram API reverse-proxy (see
    _lookup_telegram_api_proxy_url) is applied uniformly everywhere,
    instead of only some call sites remembering to check for it."""
    proxy_base = _lookup_telegram_api_proxy_url()
    session = AiohttpSession(api=TelegramAPIServer.from_base(proxy_base)) if proxy_base else None
    return Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML), session=session)


def send_message_sync(chat_id: int, text: str, timeout: float = 10.0, token: str | None = None) -> bool:
    """Thread-safe, best-effort message send for callers running OUTSIDE
    the bot's own event loop/thread (aiogram's Bot is not thread-safe to
    call directly) - e.g. the daily quota/expiry reminder job and the 4x/day
    backup job, both running synchronously on APScheduler's own thread.

    Deliberately does NOT reuse an actively-polling bot's Bot/loop - spins
    up its own short-lived Bot(token) instance instead. This matters once
    the admin can move the INTERACTIVE bot (the one doing getUpdates
    polling) to a remote server: Telegram only allows one poller per token,
    but sending messages via the Bot API has no such restriction, so this
    server can always push notifications/backups through the same token
    regardless of where the polling bot currently lives.

    `token`, when given, sends through THAT token instead of the shared/
    global BotSettings one - used for a level-2 Admin's own scoped backup
    delivered through their own dedicated bot (see services/backup.py).
    Returns False (never raises) if no token is configured/given or the
    send failed - e.g. the customer blocked the bot, which is expected
    often enough that it shouldn't be treated as an error by callers."""
    token = token or _lookup_bot_token()
    if not token:
        return False

    async def _send():
        bot = _make_bot(token)
        try:
            await asyncio.wait_for(bot.send_message(chat_id, text), timeout=timeout)
        finally:
            await bot.session.close()

    try:
        asyncio.run(_send())
        return True
    except Exception:
        return False


def send_document_sync(chat_id: int, file_path: str, caption: str = "", timeout: float = 30.0, token: str | None = None) -> bool:
    """Same idea as send_message_sync but for a file on disk - used by
    services/backup.py to deliver database backups to the bot admins.
    Longer default timeout since backup files can be a few MB. `token` -
    see send_message_sync's docstring."""
    token = token or _lookup_bot_token()
    if not token:
        return False

    async def _send():
        bot = _make_bot(token)
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
    inst: _Instance, token: str, admin_ids: set, approval_chat_ids: set, stop_event: threading.Event,
    customer_bot_enabled: bool, bot_owner_admin_id: int | None,
) -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    inst.loop = loop
    try:
        loop.run_until_complete(
            _main(inst, token, admin_ids, approval_chat_ids, stop_event, customer_bot_enabled, bot_owner_admin_id)
        )
    finally:
        inst.loop = None
        loop.close()


async def _main(
    inst: _Instance, token: str, admin_ids: set, approval_chat_ids: set, stop_event: threading.Event,
    customer_bot_enabled: bool, bot_owner_admin_id: int | None,
) -> None:
    # This thread's OWN isolated copy of RuntimeConfig (see config.py's
    # threading.local docstring) - every handler this bot instance ever
    # invokes reads back exactly these values via `from .config import
    # config`, regardless of how many OTHER bot instances are concurrently
    # doing the same thing on their own threads.
    config.configure(token, admin_ids, approval_chat_ids, customer_bot_enabled, bot_owner_admin_id)
    bot_storage.init_db()

    bot = _make_bot(token)
    try:
        me = await bot.get_me()
    except Exception as exc:
        inst.status.update(running=False, last_error=f"توکن نامعتبر است یا تلگرام در دسترس نیست: {exc}")
        logger.exception("failed to start telegram bot (owner_admin_id=%s)", bot_owner_admin_id)
        await bot.session.close()
        return

    inst.bot = bot
    inst.status.update(running=True, last_error=None, bot_username=me.username)
    logger.info("Telegram bot started: @%s (owner_admin_id=%s)", me.username, bot_owner_admin_id)
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
                    inst.status.update(running=False, last_error=str(exc))
                    logger.exception("telegram bot polling stopped unexpectedly (owner_admin_id=%s)", bot_owner_admin_id, exc_info=exc)
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
        inst.bot = None
        inst.status.update(running=False)
        logger.info("Telegram bot stopped (owner_admin_id=%s)", bot_owner_admin_id)


def start_bot(
    token: str, admin_ids: set, approval_chat_ids: set, customer_bot_enabled: bool = True,
    instance_key=_MAIN, bot_owner_admin_id: int | None = None,
) -> None:
    """Starts a bot instance on a background thread. No-op if that
    instance_key is already running - call restart_bot() to apply changed
    settings to a running bot. Omit instance_key for the shared/global bot
    (unchanged pre-hierarchy behavior); pass a level-2 Admin's id (and set
    bot_owner_admin_id to that same id) to start THEIR dedicated bot."""
    inst = _get_instance(instance_key)
    with inst.lock:
        if inst.thread and inst.thread.is_alive():
            return
        if not token or not admin_ids:
            inst.status.update(running=False, last_error="توکن ربات یا آیدی عددی ادمین تنظیم نشده است")
            return
        inst.stop_event = threading.Event()
        inst.thread = threading.Thread(
            target=_run_loop,
            args=(inst, token, set(admin_ids), set(approval_chat_ids), inst.stop_event, customer_bot_enabled, bot_owner_admin_id),
            name=f"telegram-bot-{instance_key}",
            daemon=True,
        )
        inst.thread.start()


def stop_bot(timeout: float = 10.0, instance_key=_MAIN) -> None:
    inst = _get_instance(instance_key)
    with inst.lock:
        if inst.stop_event:
            inst.stop_event.set()
        thread = inst.thread
        inst.thread = None
    if thread:
        thread.join(timeout=timeout)
    inst.status.update(running=False)


def restart_bot(
    token: str, admin_ids: set, approval_chat_ids: set, enabled: bool, customer_bot_enabled: bool = True,
    instance_key=_MAIN, bot_owner_admin_id: int | None = None,
) -> None:
    """Stops whatever is currently running under this instance_key (if
    anything) and starts fresh with the new settings - called every time
    the admin saves the bot settings page, so changes take effect
    immediately without a container restart."""
    stop_bot(instance_key=instance_key)
    if enabled and token and admin_ids:
        start_bot(token, admin_ids, approval_chat_ids, customer_bot_enabled, instance_key=instance_key, bot_owner_admin_id=bot_owner_admin_id)
    else:
        _get_instance(instance_key).status.update(running=False, last_error=None if enabled else None)


# ------------------------------------------------------- per-admin bots
def _admin_instance_key(admin_id: int):
    return ("admin", admin_id)


def start_admin_bot(admin_id: int, token: str, telegram_id: int | None) -> None:
    """Starts (or is a no-op if already running) a level-2 Admin's own
    dedicated bot - see AdminUser.own_bot_token/own_bot_enabled and
    routers/telegram_bot_settings.py's admin-facing endpoints. admin_ids
    here is ONLY this admin's own linked telegram_id (not the shared bot's
    global admin_ids list) - nobody else gets the admin command menu on
    THIS bot. If telegram_id isn't linked yet, the bot still starts (so
    customers can use it right away) but nobody gets the admin menu until
    they link their id from the ادمین‌ها page."""
    admin_ids = {telegram_id} if telegram_id else set()
    start_bot(token, admin_ids or {0}, set(), True, instance_key=_admin_instance_key(admin_id), bot_owner_admin_id=admin_id)


def stop_admin_bot(admin_id: int) -> None:
    stop_bot(instance_key=_admin_instance_key(admin_id))


def restart_admin_bot(admin_id: int, token: str, telegram_id: int | None, enabled: bool) -> None:
    admin_ids = {telegram_id} if telegram_id else set()
    restart_bot(
        token, admin_ids or {0}, set(), enabled, True,
        instance_key=_admin_instance_key(admin_id), bot_owner_admin_id=admin_id,
    )


def get_admin_bot_status(admin_id: int) -> dict:
    return get_status(instance_key=_admin_instance_key(admin_id))
