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
from ..callbacks import MenuCB
from ..config import config
from ..keyboards import cancel_kb, home_kb
from ..states import AdminBroadcastStates

router = Router(name="admin_broadcast")
router.message.filter(lambda m: config.is_admin(m.from_user.id))
router.callback_query.filter(lambda c: config.is_admin(c.from_user.id))

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
        telegram_ids = await api.list_telegram_user_ids()
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
