""""🔗 وصل کردن حساب قبلی" - a customer claims an account that already
exists on the panel (bought before the bot existed, or through a different
bot/admin). Split out of the old single customer.py (see customer_common.py's
module docstring for why).

Note: the "/link" SLASH COMMAND (cmd_link) is not here - it lives on
customer.py's own router alongside the other three slash commands, so it is
always checked before link_username's catch-all below regardless of which
section file that catch-all lives in. See customer.py's module docstring."""
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..callbacks import MenuCB
from ..panel_bridge import api, ApiError
from ..keyboards import cancel_kb, home_kb
from ..states import CustomerLinkStates
from .. import storage
from .customer_common import _menu_item_enabled, _notify_targets, _reply_menu_item_disabled

router = Router(name="customer_link")


@router.callback_query(MenuCB.filter(F.action == "cust_link"))
async def cb_link_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await _menu_item_enabled("cust_link"):
        await _reply_menu_item_disabled(call)
        return
    await state.set_state(CustomerLinkStates.waiting_username)
    await call.message.edit_text("نام کاربری حساب قبلی‌تان را بفرستید:", reply_markup=cancel_kb())
    await call.answer()


@router.message(CustomerLinkStates.waiting_username)
async def link_username(message: Message, state: FSMContext, bot: Bot) -> None:
    """Security note: this does NOT link immediately - a username alone
    proves nothing (anyone could type someone else's username and read
    their balance/services). It just files a request an admin has to
    approve/reject (see admin_pending.py's cb_approval "link" branch),
    exactly like a purchase receipt does - reuses the same pending_purchases
    table/approval flow, just with a dummy zero-price "package"."""
    username = (message.text or "").strip()
    try:
        target_user = await api.get_user(username)
    except ApiError as exc:
        await message.answer(f"خطا: {exc}\nدوباره امتحان کنید یا انصراف بدهید:", reply_markup=cancel_kb())
        return
    await state.clear()

    request_id = storage.create_pending(
        telegram_id=message.from_user.id,
        telegram_username=message.from_user.username,
        telegram_name=message.from_user.full_name,
        kind="link",
        package={"id": 0, "name": "اتصال حساب قبلی", "quota_gb": 0, "duration_days": None, "price": 0},
        target_username=username,
    )
    await message.answer(
        "✅ درخواست اتصال حساب شما برای ادمین ارسال شد. بعد از تایید ادمین، به این حساب دسترسی خواهید داشت.",
        reply_markup=home_kb(),
    )

    from .admin_pending import _pending_summary, _owner_label  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    who = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.full_name or str(message.from_user.id))
    link_pending_row = storage.get_pending(request_id)
    caption = (
        "🔗 درخواست اتصال حساب قبلی\n\n"
        + _pending_summary(link_pending_row, await _owner_label(link_pending_row.get("owner_admin_id")))
        + f"\n\nحساب مقصد: «{username}»"
        + (f" ({target_user.get('full_name')})" if target_user.get("full_name") else "")
        + f"\nموجودی فعلی آن حساب: {target_user.get('balance', 0):,} تومان"
        + f"\n\n⚠️ فقط اگر مطمئنید {who} واقعا صاحب این حساب است تایید کنید."
    )
    for admin_id in await _notify_targets(link_pending_row):
        try:
            await bot.send_message(admin_id, caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass
