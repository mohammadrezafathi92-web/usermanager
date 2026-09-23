""""👤 اکانت من" and the rest of the account-viewing menu - "کدام‌یک" account
switching, referral info, support text, per-service usage, and the numeric
telegram id lookup. Split out of the old single customer.py (see
customer_common.py's module docstring for why); the account-linking flow
("🔗 وصل کردن حساب قبلی") and the purchase/topup flows live in their own
sibling files - cb_switch_account below is the one place this file still
reaches into them, to resume whichever view the account picker interrupted.

Note: the "👤 اکانت من" SLASH COMMAND (cmd_account) is not here - it lives on
customer.py's own router alongside the other three slash commands, so it is
always checked before any catch-all state handler regardless of which
section file that catch-all lives in. See customer.py's module docstring."""
from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from ..admin_scope import resolve_admin_scope
from ..callbacks import ConnectionCB, MenuCB, PurchaseCB, SwitchAccountCB
from ..connection_sender import send_connection
from ..panel_bridge import api, ApiError
from ..keyboards import (
    connections_list_kb,
    group_connections_by_purchase,
    home_kb,
    main_menu_kb,
    purchases_kb,
    standalone_usage_text,
)
from ..utils import fmt_bytes, fmt_date_jalali, STATUS_LABELS
from .customer_common import (
    DEFAULT_PURCHASE_BLOCK_TEXT,
    _clear_state_keep_account,
    _menu_item_enabled,
    _reply_menu_item_disabled,
    _resolve_account,
    _send_menu_footer,
)
from .customer_purchase import cb_renew
from .customer_topup import cb_topup_start

router = Router(name="customer_account")


def _account_text(user: dict) -> str:
    # Lead with the actual name when the admin set one (e.g. "علی رضایی")
    # instead of the panel's internal username (which can be a cryptic
    # auto-generated "tg266249955" or a raw MikroTik-imported login) - the
    # username is still shown, just as a secondary technical detail.
    heading = user.get("full_name") or user["username"]
    lines = [f"👤 <b>{heading}</b>"]
    if user.get("full_name"):
        lines.append(f"نام کاربری: <code>{user['username']}</code>")
    lines += [
        f"وضعیت: {STATUS_LABELS.get(user['status'], user['status'])}",
        f"مصرف: {fmt_bytes(user['used_bytes'])} / {fmt_bytes(user['total_quota_bytes']) if user['total_quota_bytes'] else 'نامحدود'}",
        f"انقضا: {fmt_date_jalali(user.get('expire_at'))}",
        f"موجودی اعتبار: {user.get('balance', 0):,} تومان",
    ]
    if user.get("purchases_blocked"):
        # Said here as well as at the buttons, so the customer finds out by
        # looking at their account rather than only by being refused.
        reason = (user.get("purchases_blocked_reason") or "").strip() or DEFAULT_PURCHASE_BLOCK_TEXT
        lines.append(f"🔒 {reason}")
    if user.get("referral_code"):
        lines.append(f"🎁 کد دعوت شما: <code>{user['referral_code']}</code>")
    reserved_gb = user.get("reserved_quota_gb")
    reserved_days = user.get("reserved_duration_days")
    if reserved_gb or reserved_days:
        # A renewal was paid for while the current package still had room -
        # it's queued, not lost/forgotten - see models.User.reserved_quota_bytes's
        # docstring and services/user_ops.py's renew_user.
        parts = []
        if reserved_gb:
            parts.append(f"{reserved_gb:g} گیگابایت")
        if reserved_days:
            parts.append(f"{reserved_days} روز")
        lines.append(
            "⏳ یک تمدید (" + " و ".join(parts) + ") رزرو شده و به محض تمام شدن سرویس فعلی‌تان خودکار فعال می‌شود."
        )
    if user["connections"]:
        # Per-service usage used to be appended here too (usage_per_service_text)
        # - duplicated what the dedicated "📊 مصرف" button (cust_usage,
        # standalone_usage_text) already shows on its own, cluttering the
        # account view with numbers that belong in the usage view instead.
        # "اکانت من" now only lists which services exist / their config
        # buttons; per-service byte counts live exclusively under "مصرف".
        # service_count, not len(connections): one purchase can bundle
        # several connections (a package with WireGuard + OpenVPN + Xray is
        # ONE service, three connections), and telling a customer who bought
        # 3 packages that they have "12 سرویس" is simply false. Falls back to
        # the connection count for a legacy user with no purchases, where the
        # two are the same thing anyway.
        count = user.get("service_count") or len(user["connections"])
        lines.append(f"\n<b>خریدهای شما ({count} سرویس):</b> روی هرکدوم از دکمه‌های پایین بزنید 👇")
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.action == "cust_account"))
async def cb_account(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    if not await _menu_item_enabled("cust_account"):
        await _reply_menu_item_disabled(call)
        return
    await _clear_state_keep_account(state)
    user = await _resolve_account(call, state, call.from_user.id, "cust_account")
    if user == "ambiguous":
        return
    if not user:
        scope = await resolve_admin_scope(call.from_user.id)
        await call.message.edit_text(
            "هنوز حسابی برای شما ثبت نشده.", reply_markup=await main_menu_kb(scope)
        )
        await call.answer()
        return
    groups = group_connections_by_purchase(user["connections"]) if user["connections"] else []
    await call.message.edit_text(
        _account_text(user),
        reply_markup=purchases_kb(groups) if groups else home_kb(),
    )
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_sublink"))
async def cb_sublink(call: CallbackQuery, state: FSMContext) -> None:
    """Sends the customer their OWN subscription link (see routers/
    subscription.py) - one link that always reflects every Xray/VLESS
    service on this account combined, for pasting straight into a V2ray-
    family client instead of importing each service's link by hand.
    Deliberately account-level (see keyboards.py's purchases_kb), since the
    underlying endpoint is inherently per-User, not per-connection."""
    user = await _resolve_account(call, state, call.from_user.id, "cust_account")
    if user == "ambiguous":
        return
    if not user:
        await call.answer("ابتدا باید یک حساب داشته باشید.", show_alert=True)
        return
    try:
        link = await api.get_subscription_link(user["username"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    app_url = link.get("app_url")
    if not app_url:
        await call.answer(
            "لینک ساب هنوز توسط پشتیبانی تنظیم نشده - لطفاً با پشتیبانی تماس بگیرید.",
            show_alert=True,
        )
        return
    text = (
        "🔗 <b>لینک ساب شما</b>\n\n"
        "این لینک همیشه ثابت است و همه‌ی سرویس‌های V2ray/Xray شما را با هم نشان می‌دهد - "
        "کافی است آن را در برنامه‌ی وی‌پی‌ان خود به‌عنوان Subscribe اضافه کنید:\n\n"
        f"<code>{app_url}</code>"
    )
    await call.message.answer(text)
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_support"))
async def cb_support(call: CallbackQuery) -> None:
    """Static support text/contact the admin sets from the panel's Settings
    page (PanelSettings.support_contact_text) - see routers/panel_settings.py.
    Deliberately simple (no ticket system) per the confirmed design."""
    if not await _menu_item_enabled("cust_support"):
        await _reply_menu_item_disabled(call)
        return
    try:
        settings = await api.get_payment_info()  # returns the full PanelSettings row, not just payment fields
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    text = settings.get("support_contact_text") or "برای پشتیبانی، ادمین هنوز اطلاعات تماسی ثبت نکرده است."
    await call.message.edit_text(f"🎧 پشتیبانی\n\n{text}", reply_markup=home_kb())
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_referral"))
async def cb_referral(call: CallbackQuery, state: FSMContext) -> None:
    """Shows the customer's own invite code plus the currently-configured
    reward amounts (both sides get a gift - see PanelSettings.referral_*
    and services/user_ops.py's apply_referral_code)."""
    if not await _menu_item_enabled("cust_referral"):
        await _reply_menu_item_disabled(call)
        return
    user = await _resolve_account(call, state, call.from_user.id, "cust_referral")
    if user == "ambiguous":
        return
    if not user:
        await call.answer("ابتدا باید یک حساب داشته باشید.", show_alert=True)
        return
    try:
        settings = await api.get_payment_info()
    except ApiError:
        settings = {}
    lines = [
        "🎁 دعوت دوستان",
        "",
        f"کد دعوت شما: <code>{user.get('referral_code') or '—'}</code>",
        "این کد را برای دوستانتان بفرستید - با اولین خرید آن‌ها با این کد،",
    ]
    rewards = []
    ref_credit = settings.get("referral_referrer_reward_credit") or 0
    ref_gb = settings.get("referral_referrer_reward_gb") or 0
    if ref_credit:
        rewards.append(f"{ref_credit:,} تومان اعتبار")
    if ref_gb:
        rewards.append(f"{ref_gb:g} گیگابایت حجم")
    lines.append(("شما " + " و ".join(rewards) + " هدیه می‌گیرید،") if rewards else "شما هدیه می‌گیرید،")
    new_rewards = []
    new_credit = settings.get("referral_new_user_reward_credit") or 0
    new_gb = settings.get("referral_new_user_reward_gb") or 0
    if new_credit:
        new_rewards.append(f"{new_credit:,} تومان اعتبار")
    if new_gb:
        new_rewards.append(f"{new_gb:g} گیگابایت حجم")
    lines.append(("و خودشان هم " + " و ".join(new_rewards) + " هدیه می‌گیرند.") if new_rewards else "و خودشان هم هدیه می‌گیرند.")
    await call.message.edit_text("\n".join(lines), reply_markup=home_kb())
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_usage"))
async def cb_usage(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Dedicated top-level "📊 مصرف سرویس‌ها" button - the ONLY place a
    customer sees per-service usage numbers (standalone_usage_text) -
    "اکانت من" (_account_text above) deliberately does not repeat them,
    just lists which services exist and their config buttons."""
    if not await _menu_item_enabled("cust_usage"):
        await _reply_menu_item_disabled(call)
        return
    user = await _resolve_account(call, state, call.from_user.id, "cust_usage")
    if user == "ambiguous":
        return
    if not user:
        await call.message.edit_text("هنوز حسابی برای شما ثبت نشده.", reply_markup=home_kb())
        await call.answer()
        return
    await call.message.edit_text(standalone_usage_text(user["connections"], expire_at=user.get("expire_at")), reply_markup=home_kb())
    await call.answer()


@router.callback_query(PurchaseCB.filter())
async def cb_view_purchase(call: CallbackQuery, callback_data: PurchaseCB, state: FSMContext, bot: Bot) -> None:
    """Fires when a customer taps one multi-service purchase button under
    "👤 اکانت من" - opens a submenu listing just that purchase's services
    (single-service purchases skip this entirely - purchases_kb wires their
    button straight to ConnectionCB, same as before this feature existed)."""
    user = await _resolve_account(call, state, call.from_user.id, "cust_account")
    if user == "ambiguous":
        return
    if not user:
        await call.answer("حساب شما پیدا نشد", show_alert=True)
        return
    groups = group_connections_by_purchase(user["connections"])
    group = next((g for g in groups if g["key"] == callback_data.key), None)
    if not group:
        # That purchase's services were all removed/renamed since the
        # account view was opened (e.g. an admin deleted the connections) -
        # re-show the current list instead of erroring.
        await call.message.edit_text(_account_text(user), reply_markup=purchases_kb(groups) if groups else home_kb())
        await call.answer("لیست به‌روزرسانی شد")
        return
    await call.message.edit_text(
        group["label"],
        reply_markup=connections_list_kb(group["connections"], back_to_purchases=True),
    )
    await call.answer()


@router.callback_query(ConnectionCB.filter())
async def cb_view_connection(call: CallbackQuery, callback_data: ConnectionCB, state: FSMContext, bot: Bot) -> None:
    """Fires when a customer taps one specific service button under "👤
    اکانت من" - sends just that service's config/link/QR, instead of the
    old behavior of dumping every service automatically the moment the
    account view opened."""
    user = await _resolve_account(call, state, call.from_user.id, "cust_account")
    if user == "ambiguous":
        return
    if not user:
        await call.answer("حساب شما پیدا نشد", show_alert=True)
        return
    conn = next((c for c in user["connections"] if c["id"] == callback_data.connection_id), None)
    if not conn:
        await call.answer("این سرویس دیگر وجود ندارد", show_alert=True)
        return
    await call.answer()
    await send_connection(bot, call.from_user.id, conn)
    # The config messages are sent as NEW messages below the menu, so the
    # menu ends up scrolled off above them and the customer is left with
    # no way forward. Re-post it underneath (same thing pay_with_balance
    # already does after sending a purchase's configs).
    await _send_menu_footer(bot, call.from_user.id)


@router.callback_query(SwitchAccountCB.filter())
async def cb_switch_account(call: CallbackQuery, callback_data: SwitchAccountCB, state: FSMContext, bot: Bot) -> None:
    """Fires when a customer with several linked accounts (see
    User.telegram_id) taps one in the account-picker shown by
    _resolve_account. Remembers the choice for the rest of this session
    (state's "active_username") and resumes whichever view originally
    triggered the picker (state's "pending_menu_action", set by
    _resolve_account right before showing it)."""
    await state.update_data(active_username=callback_data.username)
    data = await state.get_data()
    action = data.get("pending_menu_action") or "cust_account"
    if action == "cust_usage":
        await cb_usage(call, state, bot)
    elif action == "cust_renew":
        await cb_renew(call, state)
    elif action == "cust_topup":
        await cb_topup_start(call, state)
    elif action == "cust_referral":
        await cb_referral(call, state)
    else:
        await cb_account(call, state, bot)


# --------------------------------------------------------------- numeric id
@router.callback_query(MenuCB.filter(F.action == "cust_myid"))
async def cb_myid(call: CallbackQuery) -> None:
    """Shows the customer their own numeric Telegram id so they can copy it
    and send it to an admin - used when an admin wants to manually link an
    account from the panel's user-edit form (see UserDetail.jsx's "آیدی
    عددی تلگرام" field) but the customer hasn't purchased/linked through the
    bot yet, so there's no other way for the admin to get this number."""
    if not await _menu_item_enabled("cust_myid"):
        await _reply_menu_item_disabled(call)
        return
    text = (
        f"🆔 آیدی عددی تلگرام شما:\n\n<code>{call.from_user.id}</code>\n\n"
        "روی عدد بزنید تا کپی شود، و آن را برای ادمین ارسال کنید."
    )
    await call.message.edit_text(text, reply_markup=home_kb())
    await call.answer()
