"""Admin "📢 پیام همگانی" flow - type a message, confirm, and it gets sent
to every customer currently linked to a telegram id (i.e. everyone who has
used the bot at least once - either bought through it or used "وصل کردن
حساب قبلی"). A small delay between sends keeps this well under Telegram's
per-bot rate limits even for a few hundred recipients."""
import asyncio
import logging

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger("telegram_bot")

from ..panel_bridge import api, ApiError
from ..admin_scope import resolve_admin_scope
from ..callbacks import MenuCB
from ..config import config
from ..keyboards import cancel_kb, home_kb
from ..states import AdminBroadcastStates, AdminDirectMessageStates

router = Router(name="admin_broadcast")
async def _is_admin_filter(event) -> bool:
    """Async on purpose - NOT a plain lambda. See git history / chat log for
    why: aiogram offloads sync filter callables to a background executor
    thread, and config.RuntimeConfig is a threading.local, so a sync lambda
    referencing config.is_admin() silently always returns False there.
    Matches admin_users.py's _admin_scope_filter, which already had to be
    async for the same reason.

    Resolved through admin_scope, not config.is_admin: broadcasting is now
    available to level-2 Admins too, scoped to their own tree by the
    recipient query rather than by who can reach this router."""
    scope = await resolve_admin_scope(event.from_user.id)
    return bool(scope and scope["is_full_admin"])
router.message.filter(_is_admin_filter)
router.callback_query.filter(_is_admin_filter)

SEND_DELAY_SECONDS = 0.05  # ~20 messages/sec - well under Telegram's bot rate limits


def _confirm_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ ارسال شود", callback_data=MenuCB(action="broadcast_send"))
    kb.button(text="✖️ انصراف", callback_data=MenuCB(action="cancel"))
    kb.adjust(2)
    return kb.as_markup()


@router.callback_query(MenuCB.filter(F.action == "admin_broadcast"))
async def cb_broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminBroadcastStates.waiting_text)
    await call.message.edit_text(
        "متن پیامی که می‌خواهید برای همه کاربران ربات ارسال شود را بفرستید:",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(Command("broadcast"))
async def cmd_broadcast_start(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "📢 پیام همگانی"."""
    await state.set_state(AdminBroadcastStates.waiting_text)
    await message.answer(
        "متن پیامی که می‌خواهید برای همه کاربران ربات ارسال شود را بفرستید:",
        reply_markup=cancel_kb(),
    )


@router.message(AdminBroadcastStates.waiting_text)
async def receive_broadcast_text(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption
    if not text:
        await message.answer("لطفا یک پیام متنی بفرستید:", reply_markup=cancel_kb())
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminBroadcastStates.waiting_confirm)
    await message.answer(
        f"پیش‌نمایش پیام:\n\n{text}\n\nاین پیام برای همه کاربران ربات ارسال شود؟",
        reply_markup=_confirm_kb(),
    )


@router.callback_query(MenuCB.filter(F.action == "broadcast_send"), AdminBroadcastStates.waiting_confirm)
async def cb_broadcast_send(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = data.get("broadcast_text")
    await state.clear()
    if not text:
        await call.answer("پیامی برای ارسال پیدا نشد", show_alert=True)
        return

    try:
        scope = await resolve_admin_scope(call.from_user.id)
        telegram_ids = await api.list_telegram_user_ids(
            owner_admin_id=(scope or {}).get("owner_admin_id")
        )
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return

    await call.message.edit_text(f"⏳ در حال ارسال به {len(telegram_ids)} کاربر...")
    await call.answer()

    sent = 0
    failed = 0
    for tg_id in telegram_ids:
        if tg_id == call.from_user.id:
            continue  # don't send the admin their own broadcast
        try:
            # Explicit parse_mode=None: the bot's default is HTML, and a
            # broadcast is free-form admin text that may contain stray
            # "<"/"&" - sending as HTML would fail for every single
            # recipient with an indistinguishable "failed" count and no way
            # to tell a bad message apart from blocked/deleted accounts.
            await bot.send_message(tg_id, text, parse_mode=None)
            sent += 1
        except Exception as exc:
            failed += 1
            logger.warning("broadcast send to %s failed: %s", tg_id, exc)
        await asyncio.sleep(SEND_DELAY_SECONDS)

    await call.message.answer(
        f"✅ پیام همگانی ارسال شد.\n\nموفق: {sent}\nناموفق: {failed}\nمجموع: {len(telegram_ids)}",
        reply_markup=home_kb(),
    )


# --------------------------------------------------------------------------
# «✉️ پیام به یک کاربر» - the targeted counterpart to the broadcast above:
# the admin names ONE customer and sends just them a message. Useful for
# support replies ("رسیدت رو دیدم، تا یک ساعت دیگه فعال میشه") without
# needing that customer's raw telegram id or an outside chat.
# --------------------------------------------------------------------------

async def _start_dm(target, state: FSMContext) -> None:
    await state.set_state(AdminDirectMessageStates.waiting_target)
    await _reply_dm(
        target,
        "✉️ نام کاربری مشتری را بفرستید (یا شناسه عددی تلگرامش را).\n\n"
        "مثال: <code>tg266249955</code>",
    )


async def _reply_dm(target, text: str) -> None:
    """The entry points are a CallbackQuery (menu button) and a Message
    (slash command) - reply correctly for either, same idea as
    handlers/customer.py's _reply."""
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=cancel_kb())
        await target.answer()
    else:
        await target.answer(text, reply_markup=cancel_kb())


@router.callback_query(MenuCB.filter(F.action == "admin_dm"))
async def cb_dm_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _start_dm(call, state)


@router.message(Command("dm"))
async def cmd_dm_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await _start_dm(message, state)


@router.message(AdminDirectMessageStates.waiting_target, F.text)
async def receive_dm_target(message: Message, state: FSMContext) -> None:
    """Resolves what the admin typed into a real customer + telegram id.
    Accepts either a panel username or a raw numeric telegram id, and
    refuses to continue unless the customer actually has a linked telegram
    account (there'd be nowhere to deliver the message otherwise)."""
    raw = (message.text or "").strip().lstrip("@")

    user = None
    if raw.isdigit():
        user = await api.get_user_by_telegram(int(raw))
        if not user:
            await message.answer(
                "کاربری با این شناسه تلگرام پیدا نشد. دوباره امتحان کنید یا انصراف بدهید:",
                reply_markup=cancel_kb(),
            )
            return
    else:
        try:
            user = await api.get_user(raw)
        except ApiError:
            await message.answer(
                "کاربری با این نام کاربری پیدا نشد. دوباره امتحان کنید یا انصراف بدهید:",
                reply_markup=cancel_kb(),
            )
            return

    if not user.get("telegram_id"):
        await message.answer(
            f"حساب «{user['username']}» به هیچ اکانت تلگرامی وصل نیست، پس نمی‌شود پیامی برایش فرستاد.",
            reply_markup=home_kb(),
        )
        await state.clear()
        return

    await state.update_data(dm_username=user["username"], dm_telegram_id=user["telegram_id"])
    await state.set_state(AdminDirectMessageStates.waiting_text)
    await message.answer(
        f"✅ مقصد: <b>{user['username']}</b>\n\nحالا متن پیام را بفرستید:",
        reply_markup=cancel_kb(),
    )


@router.message(AdminDirectMessageStates.waiting_text, F.text)
async def receive_dm_text(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    tg_id = data.get("dm_telegram_id")
    username = data.get("dm_username")
    if not tg_id:
        await state.clear()
        await message.answer("مقصد پیام پیدا نشد - دوباره از منو شروع کنید.", reply_markup=home_kb())
        return

    await state.clear()
    try:
        # parse_mode=None for the same reason as the broadcast above: this
        # is free-form admin text that may contain a stray "<" or "&".
        await bot.send_message(tg_id, message.text, parse_mode=None)
    except Exception as exc:
        logger.warning("direct message to %s (%s) failed: %s", username, tg_id, exc)
        await message.answer(
            f"❌ ارسال پیام به «{username}» ناموفق بود (احتمالا ربات را بلاک کرده است).",
            reply_markup=home_kb(),
        )
        return

    await message.answer(f"✅ پیام برای «{username}» ارسال شد.", reply_markup=home_kb())
