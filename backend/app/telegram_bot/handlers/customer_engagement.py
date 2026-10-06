"""Customer-facing agent application and read-only service tariff pages."""
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..callbacks import MenuCB
from ..config import config
from ..keyboards import cancel_kb, home_kb
from ..panel_bridge import ApiError, api
from ..states import CustomerAgentRequestStates
from ..utils import packages_message
from .customer_common import _menu_item_enabled, _reply_menu_item_disabled

logger = logging.getLogger("telegram_bot")
router = Router(name="customer_engagement")


@router.callback_query(MenuCB.filter(F.action == "cust_prices"))
async def cb_prices(call: CallbackQuery, state: FSMContext) -> None:
    if not await _menu_item_enabled("cust_prices"):
        await _reply_menu_item_disabled(call)
        return
    await state.clear()
    try:
        packages = await api.list_packages()
    except ApiError as exc:
        await call.answer(f"خطا در دریافت تعرفه‌ها: {exc}", show_alert=True)
        return
    if not packages:
        text = "در حال حاضر تعرفه‌ای برای فروش ثبت نشده است."
    else:
        text = packages_message(packages, title="📋 <b>تعرفه سرویس‌ها</b>")
    await call.message.edit_text(text, reply_markup=home_kb())
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_agent"))
async def cb_agent_request(call: CallbackQuery, state: FSMContext) -> None:
    if not await _menu_item_enabled("cust_agent"):
        await _reply_menu_item_disabled(call)
        return
    await state.clear()
    await state.set_state(CustomerAgentRequestStates.waiting_message)
    await call.message.edit_text(
        "🤝 <b>درخواست نمایندگی</b>\n\n"
        "لطفاً نام، راه ارتباطی و توضیح کوتاهی درباره‌ی درخواست‌تان را در یک پیام بفرستید.\n"
        "اطلاعات برای مدیر همین ربات ارسال می‌شود.",
        reply_markup=cancel_kb(),
    )
    await call.answer()


@router.message(CustomerAgentRequestStates.waiting_message, F.text)
async def receive_agent_request(message: Message, state: FSMContext, bot: Bot) -> None:
    text = (message.text or "").strip()
    if text.startswith("/cancel"):
        await state.clear()
        await message.answer("درخواست لغو شد.", reply_markup=home_kb())
        return
    if not text or len(text) > 2000:
        await message.answer("متن باید بین ۱ تا ۲۰۰۰ نویسه باشد؛ دوباره بفرستید.", reply_markup=cancel_kb())
        return

    targets = config.approval_targets()
    if not targets:
        await state.clear()
        await message.answer("در حال حاضر مقصدی برای دریافت درخواست نمایندگی تنظیم نشده است.", reply_markup=home_kb())
        return

    user = message.from_user
    name = " ".join(part for part in (user.full_name, f"@{user.username}" if user.username else None) if part)
    notification = (
        "🤝 <b>درخواست نمایندگی جدید</b>\n"
        f"نام: {escape(name) if name else '—'}\n"
        f"Telegram ID: <code>{user.id}</code>\n\n"
        f"{escape(text)}"
    )
    sent = 0
    for target in targets:
        try:
            await bot.send_message(target, notification)
            sent += 1
        except Exception:
            logger.exception("ارسال درخواست نمایندگی به مدیر %s ناموفق بود", target)
    await state.clear()
    if sent:
        await message.answer("درخواست شما برای مدیر ارسال شد؛ در صورت نیاز با شما تماس می‌گیرد.", reply_markup=home_kb())
    else:
        await message.answer("ارسال درخواست انجام نشد؛ لطفاً کمی بعد دوباره تلاش کنید.", reply_markup=home_kb())


@router.message(CustomerAgentRequestStates.waiting_message)
async def reject_non_text_agent_request(message: Message) -> None:
    await message.answer("لطفاً درخواست را به‌صورت متن بفرستید یا /cancel را بزنید.", reply_markup=cancel_kb())
