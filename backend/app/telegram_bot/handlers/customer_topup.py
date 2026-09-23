""""💰 افزایش اعتبار" - top up an existing account's wallet balance via a
card-to-card receipt (no balance-pay shortcut here, unlike the purchase
flow - a top-up IS the balance, so there's nothing to pay it from). Split
out of the old single customer.py (see customer_common.py's module docstring
for why).

Note: the "/topup" SLASH COMMAND (cmd_topup) is not here - it lives on
customer.py's own router alongside the other three slash commands, so it is
always checked before any catch-all state handler regardless of which
section file that catch-all lives in. See customer.py's module docstring."""
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..callbacks import MenuCB, TopupAmountCB
from ..panel_bridge import api, ApiError
from ..keyboards import cancel_kb, home_kb, topup_amounts_kb
from ..states import CustomerTopupStates
from .. import storage
from .customer_common import (
    NO_PAYMENT_METHOD,
    _menu_item_enabled,
    _notify_targets,
    _reply_menu_item_disabled,
    _resolve_account,
)

router = Router(name="customer_topup")


async def _ask_for_topup_receipt(target, state: FSMContext, amount: int) -> None:
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        if isinstance(target, CallbackQuery):
            await target.answer(f"خطا: {exc}", show_alert=True)
        else:
            await target.answer(f"خطا: {exc}")
        return
    await state.update_data(topup_amount=amount, payment_card_id=payment.get("resolved_payment_card_id"))
    # A top-up has no wallet alternative - a card is the only way in, so
    # without one there is nothing to walk the customer through.
    if not payment.get("payment_card_number"):
        await state.clear()
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(NO_PAYMENT_METHOD)
            await target.answer()
        else:
            await target.answer(NO_PAYMENT_METHOD, reply_markup=home_kb())
        return
    lines = [
        f"مبلغ افزایش اعتبار: <b>{amount:,} تومان</b>",
        "",
        "لطفا مبلغ را به شماره کارت زیر واریز کنید و سپس عکس رسید را همینجا ارسال کنید:",
        "",
        f"💳 <code>{payment['payment_card_number']}</code>",
    ]
    if payment.get("payment_card_holder"):
        lines.append(f"به نام: {payment['payment_card_holder']}")
    if payment.get("payment_instructions"):
        lines.append("\n" + payment["payment_instructions"])
    await state.set_state(CustomerTopupStates.waiting_receipt)
    text = "\n".join(lines)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=cancel_kb())
        await target.answer()
    else:
        await target.answer(text, reply_markup=cancel_kb())


@router.callback_query(MenuCB.filter(F.action == "cust_topup"))
async def cb_topup_start(call: CallbackQuery, state: FSMContext) -> None:
    if not await _menu_item_enabled("cust_topup"):
        await _reply_menu_item_disabled(call)
        return
    account = await _resolve_account(call, state, call.from_user.id, "cust_topup")
    if account == "ambiguous":
        return
    if not account:
        await call.answer("ابتدا باید یک حساب داشته باشید یا حساب قبلی را وصل کنید.", show_alert=True)
        return
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    presets = []
    for part in (payment.get("topup_presets") or "").split(","):
        part = part.strip()
        if part.isdigit():
            presets.append(int(part))
    await state.update_data(target_username=account["username"])
    await state.set_state(CustomerTopupStates.picking_amount)
    await call.message.edit_text(
        f"موجودی فعلی: {account.get('balance', 0):,} تومان\n\nچقدر می‌خواهید اعتبار اضافه کنید؟",
        reply_markup=topup_amounts_kb(presets),
    )
    await call.answer()


@router.callback_query(TopupAmountCB.filter(), CustomerTopupStates.picking_amount)
async def pick_topup_amount(call: CallbackQuery, callback_data: TopupAmountCB, state: FSMContext) -> None:
    if callback_data.amount == 0:
        await state.set_state(CustomerTopupStates.waiting_custom_amount)
        await call.message.edit_text("مبلغ دلخواه را به تومان بفرستید (فقط عدد، مثلا 75000):", reply_markup=cancel_kb())
        await call.answer()
        return
    await _ask_for_topup_receipt(call, state, callback_data.amount)


@router.message(CustomerTopupStates.waiting_custom_amount)
async def topup_custom_amount(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip().replace(",", "")
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer("یک عدد صحیح و مثبت بفرستید (مثلا 75000):")
        return
    await _ask_for_topup_receipt(message, state, int(raw))


@router.message(CustomerTopupStates.waiting_receipt, F.photo)
async def receive_topup_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    amount = data["topup_amount"]
    target_username = data["target_username"]

    request_id = storage.create_pending(
        telegram_id=message.from_user.id,
        telegram_username=message.from_user.username,
        telegram_name=message.from_user.full_name,
        kind="topup",
        package={"id": 0, "name": f"افزایش اعتبار {amount:,} تومان", "quota_gb": 0, "duration_days": None, "price": amount},
        target_username=target_username,
        receipt_file_id=message.photo[-1].file_id,
        payment_card_id=data.get("payment_card_id"),
    )
    await state.clear()
    await message.answer("✅ رسید شما ثبت شد و برای بررسی ادمین ارسال شد. نتیجه به همین چت اطلاع داده می‌شود.", reply_markup=home_kb())

    from .admin_pending import _pending_summary, _owner_label  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    topup_pending_row = storage.get_pending(request_id)
    caption = "🧾 رسید افزایش اعتبار\n\n" + _pending_summary(topup_pending_row, await _owner_label(topup_pending_row.get("owner_admin_id")))
    for admin_id in await _notify_targets(topup_pending_row):
        try:
            await bot.send_photo(admin_id, message.photo[-1].file_id, caption=caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass


@router.message(CustomerTopupStates.waiting_receipt)
async def receive_topup_receipt_wrong_type(message: Message) -> None:
    await message.answer("لطفا عکس رسید پرداخت را ارسال کنید (نه متن).", reply_markup=cancel_kb())
