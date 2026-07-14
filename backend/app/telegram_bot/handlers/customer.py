import uuid

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB, PackageCB, SessionCountCB, NodeCB, ProtocolCB, TopupAmountCB, PayCB, ConnectionCB, PurchaseCB, SwitchAccountCB
from ..config import config
from ..admin_scope import resolve_admin_scope
from ..keyboards import (
    packages_kb,
    session_count_kb,
    nodes_kb,
    protocols_kb,
    cancel_kb,
    home_kb,
    main_menu_kb,
    topup_amounts_kb,
    receipt_choice_kb,
    promo_skip_kb,
    connections_list_kb,
    group_connections_by_purchase,
    purchases_kb,
    usage_per_service_text,
    standalone_usage_text,
    account_picker_kb,
)
from ..states import CustomerLinkStates, CustomerPurchaseStates, CustomerTopupStates
from ..utils import fmt_bytes, fmt_date, fmt_date_jalali, STATUS_LABELS
from .. import storage
from ..connection_sender import send_connection, send_connections

router = Router(name="customer")
# Everything in this file is customer-facing - admins can use it too (no
# harm), but the "no account yet" messaging is written for customers.


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
        lines.append(f"\n<b>خریدهای شما ({len(user['connections'])} سرویس):</b> روی هرکدوم از دکمه‌های پایین بزنید 👇")
        lines.append(usage_per_service_text(user["connections"]))
    return "\n".join(lines)


def _loyalty_reward_text(user: dict) -> str:
    """One-shot "🎁 loyalty reward!" line - only ever non-empty right after
    a purchase/renewal that crossed PanelSettings.loyalty_purchase_threshold
    (see services/user_ops.py's _maybe_grant_loyalty_reward and
    BotUserResponse.loyalty_reward_credit/_gb, both transient/one-time)."""
    parts = []
    credit = user.get("loyalty_reward_credit")
    gb = user.get("loyalty_reward_gb")
    if credit:
        parts.append(f"{credit:,} تومان اعتبار")
    if gb:
        parts.append(f"{gb:g} گیگابایت حجم")
    if not parts:
        return ""
    return "🎁 به‌خاطر خرید مکرر شما هدیه‌ی وفاداری دریافت کردید: " + " و ".join(parts) + "!"


async def _clear_state_keep_account(state: FSMContext) -> None:
    """state.clear() wipes ALL FSM data, including which account a customer
    with several linked accounts (see User.telegram_id in models.py) had
    already picked this session - re-save that one key right after so
    jumping between main-menu buttons doesn't re-open the account picker on
    every single tap."""
    data = await state.get_data()
    active = data.get("active_username")
    await state.clear()
    if active:
        await state.update_data(active_username=active)


async def _resolve_account(target, state: FSMContext, tg_id: int, action: str):
    """Central "which account does this customer mean" lookup, used
    everywhere a customer-facing handler needs to act on `the` account
    (view it, renew it, top it up, ...). A telegram id can now be linked to
    more than one panel account (bought more than once under different
    usernames - see User.telegram_id) so this isn't always a single answer.

    Returns:
      - a user dict - either the only account linked, or one of several
        where the customer already picked this session (state's
        "active_username")
      - None - this telegram id has no linked account at all; caller shows
        its own usual "هنوز حسابی ندارید" message
      - the string "ambiguous" - 2+ accounts linked, none picked yet; an
        account-picker keyboard has ALREADY been shown (tagged with
        `action` so cb_switch_account below knows what to resume once one
        is picked) - caller should just `return` with no further messaging

    `target` is the Message or CallbackQuery that triggered the lookup,
    only used to know how to render the "ambiguous" picker."""
    try:
        accounts = await api.list_users_by_telegram(tg_id)
    except ApiError:
        accounts = []
    if not accounts:
        return None
    if len(accounts) == 1:
        return accounts[0]
    data = await state.get_data()
    active = data.get("active_username")
    if active:
        match = next((a for a in accounts if a["username"] == active), None)
        if match:
            return match
    await state.update_data(pending_menu_action=action)
    text = "شما چند حساب دارید - کدام‌یک را می‌خواهید؟"
    kb = account_picker_kb(accounts)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=kb)
        await target.answer()
    else:
        await target.answer(text, reply_markup=kb)
    return "ambiguous"


async def _resolve_account_silent(state: FSMContext, tg_id: int):
    """Same account resolution as _resolve_account, but never interrupts
    with a picker - used where the account is a nice-to-have (prefilling
    "pay from balance"/target_username while picking a NEW purchase)
    instead of the whole point of the action, so an undecided multi-account
    customer just doesn't get that convenience instead of having their
    purchase flow hijacked by an unrelated picker."""
    try:
        accounts = await api.list_users_by_telegram(tg_id)
    except ApiError:
        return None
    if not accounts:
        return None
    if len(accounts) == 1:
        return accounts[0]
    data = await state.get_data()
    active = data.get("active_username")
    if active:
        return next((a for a in accounts if a["username"] == active), None)
    return None


async def send_package_extras(bot: Bot, chat_id: int, pkg: dict) -> None:
    """Sends whatever the admin attached to this package in "پکیج‌ها"
    (a custom message + any files - VPN configs, setup guides, installers,
    ...) to the customer, right after a successful purchase/renewal - in
    addition to the connection links the caller already sent. Best-effort:
    a missing/unreadable file or a blocked chat shouldn't blow up the
    purchase flow that already succeeded on the panel side."""
    if pkg.get("custom_message"):
        try:
            await bot.send_message(chat_id, pkg["custom_message"])
        except Exception:
            pass
    try:
        files = await api.get_package_files(pkg["id"])
    except ApiError:
        files = []
    for f in files:
        try:
            await bot.send_document(chat_id, BufferedInputFile(f["content"], filename=f["filename"]))
        except Exception:
            pass


# --------------------------------------------------------- slash commands
# Registered here, BEFORE any state-matching catch-all handler below (e.g.
# `link_username`, which matches literally any text while in
# CustomerLinkStates.waiting_username) - aiogram tries handlers in
# registration order per router, so if these were declared further down,
# typing e.g. "/buy" in the middle of another flow would get swallowed by
# that flow's catch-all instead of actually jumping to the buy flow. Each
# one clears any in-progress state first so a command always acts as a
# clean jump, matching what happens when this project's own README/help
# text implies about commands - see also runner.py's set_my_commands for
# where these show up in Telegram's native "Menu" button.
@router.message(Command("account"))
async def cmd_account(message: Message, state: FSMContext, bot: Bot) -> None:
    """Slash-command shortcut for "👤 اکانت من"."""
    await _clear_state_keep_account(state)
    user = await _resolve_account(message, state, message.from_user.id, "cust_account")
    if user == "ambiguous":
        return
    if not user:
        scope = await resolve_admin_scope(message.from_user.id)
        await message.answer("هنوز حسابی برای شما ثبت نشده.", reply_markup=await main_menu_kb(scope))
        return
    groups = group_connections_by_purchase(user["connections"]) if user["connections"] else []
    await message.answer(
        _account_text(user),
        reply_markup=purchases_kb(groups) if groups else home_kb(),
    )


@router.message(Command("link"))
async def cmd_link(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "🔗 وصل کردن حساب قبلی"."""
    await state.clear()
    await state.set_state(CustomerLinkStates.waiting_username)
    await message.answer("نام کاربری حساب قبلی‌تان را بفرستید:", reply_markup=cancel_kb())


def _distinct_session_counts(packages: list[dict]) -> list[int]:
    """Every distinct Package.max_concurrent_sessions value present among
    the given packages, sorted ascending (0 stands in for None/unlimited).
    Used to decide whether the "چند کاربره می‌خواهید؟" step is worth
    showing at all - if every available package shares the same limit (the
    common case for an admin selling only single-user packages, say),
    skipping straight to the package list avoids an extra, pointless tap."""
    return sorted({(p.get("max_concurrent_sessions") or 0) for p in packages})


@router.message(Command("buy"))
async def cmd_buy(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "🛒 خرید اکانت جدید"."""
    await state.clear()
    try:
        packages = await api.list_packages()
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
        return
    if not packages:
        await message.answer("در حال حاضر پکیجی برای فروش تعریف نشده.", reply_markup=home_kb())
        return
    await state.update_data(kind="new", packages={p["id"]: p for p in packages})
    counts = _distinct_session_counts(packages)
    if len(counts) > 1:
        await state.set_state(CustomerPurchaseStates.picking_session_count)
        await message.answer("چند کاربره می‌خواهید؟", reply_markup=session_count_kb(counts, "new"))
        return
    await state.set_state(CustomerPurchaseStates.picking_package)
    await message.answer("یک پکیج انتخاب کنید:", reply_markup=packages_kb(packages, "new"))


@router.message(Command("topup"))
async def cmd_topup(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "💰 افزایش اعتبار"."""
    await _clear_state_keep_account(state)
    account = await _resolve_account(message, state, message.from_user.id, "cust_topup")
    if account == "ambiguous":
        return
    if not account:
        await message.answer("ابتدا باید یک حساب داشته باشید یا حساب قبلی را وصل کنید.")
        return
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
        return
    presets = []
    for part in (payment.get("topup_presets") or "").split(","):
        part = part.strip()
        if part.isdigit():
            presets.append(int(part))
    await state.update_data(target_username=account["username"])
    await state.set_state(CustomerTopupStates.picking_amount)
    await message.answer(
        f"موجودی فعلی: {account.get('balance', 0):,} تومان\n\nچقدر می‌خواهید اعتبار اضافه کنید؟",
        reply_markup=topup_amounts_kb(presets),
    )


@router.callback_query(MenuCB.filter(F.action == "cust_account"))
async def cb_account(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
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


@router.callback_query(MenuCB.filter(F.action == "cust_support"))
async def cb_support(call: CallbackQuery) -> None:
    """Static support text/contact the admin sets from the panel's Settings
    page (PanelSettings.support_contact_text) - see routers/panel_settings.py.
    Deliberately simple (no ticket system) per the confirmed design."""
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
    """Dedicated top-level "📊 مصرف سرویس‌ها" button - shows the same
    per-service usage breakdown as the section under "اکانت من"
    (usage_per_service_text), but as its own standalone view reachable in
    one tap instead of having to open the account view first."""
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
    text = (
        f"🆔 آیدی عددی تلگرام شما:\n\n<code>{call.from_user.id}</code>\n\n"
        "روی عدد بزنید تا کپی شود، و آن را برای ادمین ارسال کنید."
    )
    await call.message.edit_text(text, reply_markup=home_kb())
    await call.answer()


# --------------------------------------------------------------- link existing
@router.callback_query(MenuCB.filter(F.action == "cust_link"))
async def cb_link_start(call: CallbackQuery, state: FSMContext) -> None:
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

    from .admin_pending import _pending_summary  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    who = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.full_name or str(message.from_user.id))
    caption = (
        "🔗 درخواست اتصال حساب قبلی\n\n"
        + _pending_summary(storage.get_pending(request_id))
        + f"\n\nحساب مقصد: «{username}»"
        + (f" ({target_user.get('full_name')})" if target_user.get("full_name") else "")
        + f"\nموجودی فعلی آن حساب: {target_user.get('balance', 0):,} تومان"
        + f"\n\n⚠️ فقط اگر مطمئنید {who} واقعا صاحب این حساب است تایید کنید."
    )
    for admin_id in config.approval_targets():
        try:
            await bot.send_message(admin_id, caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass


# ----------------------------------------------------------------- purchase
async def _start_package_picker(call: CallbackQuery, state: FSMContext, kind: str) -> None:
    try:
        packages = await api.list_packages()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not packages:
        await call.message.edit_text("در حال حاضر پکیجی برای فروش تعریف نشده.", reply_markup=home_kb())
        await call.answer()
        return
    await state.update_data(kind=kind, packages={p["id"]: p for p in packages})
    counts = _distinct_session_counts(packages)
    if len(counts) > 1:
        # More than one concurrent-session limit is on offer - ask first,
        # same as cmd_buy above, instead of dumping every package regardless
        # of how many people are meant to share it (see keyboards.session_count_kb).
        await state.set_state(CustomerPurchaseStates.picking_session_count)
        await call.message.edit_text("چند کاربره می‌خواهید؟", reply_markup=session_count_kb(counts, kind))
        await call.answer()
        return
    await state.set_state(CustomerPurchaseStates.picking_package)
    await call.message.edit_text("یک پکیج انتخاب کنید:", reply_markup=packages_kb(packages, kind))
    await call.answer()


@router.callback_query(SessionCountCB.filter(), CustomerPurchaseStates.picking_session_count)
async def pick_session_count(call: CallbackQuery, callback_data: SessionCountCB, state: FSMContext) -> None:
    data = await state.get_data()
    packages = list((data.get("packages") or {}).values())
    filtered = [p for p in packages if (p.get("max_concurrent_sessions") or 0) == callback_data.count]
    if not filtered:
        await call.answer("پکیجی با این تعداد کاربر پیدا نشد", show_alert=True)
        return
    await state.set_state(CustomerPurchaseStates.picking_package)
    await call.message.edit_text("یک پکیج انتخاب کنید:", reply_markup=packages_kb(filtered, callback_data.kind))
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_buy"))
async def cb_buy(call: CallbackQuery, state: FSMContext) -> None:
    await _start_package_picker(call, state, "new")


@router.callback_query(MenuCB.filter(F.action == "cust_renew"))
async def cb_renew(call: CallbackQuery, state: FSMContext) -> None:
    account = await _resolve_account(call, state, call.from_user.id, "cust_renew")
    if account == "ambiguous":
        return
    if not account:
        await call.answer("ابتدا باید یک حساب داشته باشید یا حساب قبلی را وصل کنید.", show_alert=True)
        return
    await state.update_data(target_username=account["username"])
    await _start_package_picker(call, state, "renew")


async def _reply(target, text: str, markup=None) -> None:
    """Sends `text` whether `target` is the CallbackQuery that triggered
    this step (edits the existing message, like every other handler in this
    file) or a Message the customer just sent while typing a referral/
    discount code (sends a fresh one instead - there's no message of ours
    to edit in that case). Mirrors the isinstance() branch _ask_for_topup_
    receipt below already used for the exact same reason."""
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def _ask_for_receipt(call: CallbackQuery, state: FSMContext) -> None:
    """Entry point once a package (+ node/protocol, if needed) has been
    picked - resolves the customer's existing account (if any) once here,
    then hands off to _advance_purchase_flow for the referral-code /
    discount-code / payment-screen sequence (see states.py's
    CustomerPurchaseStates for the states involved)."""
    account = await _resolve_account_silent(state, call.from_user.id)
    if account:
        await state.update_data(target_username=account["username"])
    await _advance_purchase_flow(call, state)


async def _advance_purchase_flow(target, state: FSMContext) -> None:
    """One small state machine covering the two optional pre-payment steps:
    1) referral code - ONLY for a brand-new "new" purchase (no
       target_username resolved yet - an existing customer can't be
       referred after the fact, see services/user_ops.py's
       apply_referral_code being one-shot).
    2) discount code - shown to everyone, once per purchase.
    Each step is skippable via promo_skip_kb's "⏭ رد کردن" button. Once
    both are done (or skipped), falls through to _show_payment_screen.
    Called both right after package/protocol selection (a CallbackQuery)
    and after the customer types a code or taps skip (either a CallbackQuery
    or a Message) - see _reply above for how both are handled uniformly."""
    data = await state.get_data()

    if data.get("kind") == "new" and not data.get("target_username") and not data.get("referral_step_done"):
        await state.update_data(referral_step_done=True)
        await state.set_state(CustomerPurchaseStates.entering_referral_code)
        await _reply(
            target,
            "🎁 اگر یک کد دعوت دارید همینجا تایپ کنید - هم شما هم معرفتان هدیه می‌گیرید.\n\nدر غیر این صورت دکمه رد کردن را بزنید.",
            promo_skip_kb(),
        )
        return

    if not data.get("discount_step_done"):
        await state.update_data(discount_step_done=True)
        await state.set_state(CustomerPurchaseStates.entering_discount_code)
        await _reply(
            target,
            "🎟 اگر کد تخفیف دارید همینجا تایپ کنید.\n\nدر غیر این صورت دکمه رد کردن را بزنید.",
            promo_skip_kb(),
        )
        return

    await _show_payment_screen(target, state)


@router.callback_query(MenuCB.filter(F.action == "promo_skip"), CustomerPurchaseStates.entering_referral_code)
async def skip_referral_code(call: CallbackQuery, state: FSMContext) -> None:
    await _advance_purchase_flow(call, state)


@router.message(CustomerPurchaseStates.entering_referral_code, F.text)
async def enter_referral_code(message: Message, state: FSMContext) -> None:
    await state.update_data(referral_code=message.text.strip())
    await _advance_purchase_flow(message, state)


@router.callback_query(MenuCB.filter(F.action == "promo_skip"), CustomerPurchaseStates.entering_discount_code)
async def skip_discount_code(call: CallbackQuery, state: FSMContext) -> None:
    await _advance_purchase_flow(call, state)


@router.message(CustomerPurchaseStates.entering_discount_code, F.text)
async def enter_discount_code(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pkg = data["packages"][data["package_id"]]
    code = message.text.strip()
    try:
        result = await api.validate_discount(code, package_price=pkg["price"], username=data.get("target_username"))
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
        return
    if not result.get("valid"):
        await message.answer(f"❌ {result.get('reason') or 'کد تخفیف نامعتبر است'}\n\nدوباره امتحان کنید یا رد کردن را بزنید.", reply_markup=promo_skip_kb())
        return
    await state.update_data(discount_code=code, discount_amount=result.get("discount_amount") or 0)
    await message.answer(f"✅ کد تخفیف اعمال شد: {result.get('discount_amount', 0):,} تومان تخفیف")
    await _advance_purchase_flow(message, state)


async def _show_payment_screen(target, state: FSMContext) -> None:
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        if isinstance(target, CallbackQuery):
            await target.answer(f"خطا: {exc}", show_alert=True)
        else:
            await target.answer(f"خطا: {exc}")
        return
    data = await state.get_data()
    pkg = data["packages"][data["package_id"]]
    price = pkg["price"]
    discount_amount = data.get("discount_amount") or 0
    final_price = max(0, price - discount_amount)

    # If the customer already has a linked account with enough balance,
    # offer an instant "pay from balance" option alongside the usual
    # card-to-card receipt flow (see pay_with_balance below).
    account = await _resolve_account_silent(state, _uid(target))
    can_pay_from_balance = bool(account and final_price and (account.get("balance") or 0) >= final_price)
    if account:
        await state.update_data(target_username=account["username"])

    if discount_amount:
        lines = [f"پکیج: <b>{pkg['name']}</b> — <s>{price:,}</s> {final_price:,} تومان (🎟 {discount_amount:,} تومان تخفیف)", ""]
    else:
        lines = [f"پکیج: <b>{pkg['name']}</b> — {price:,} تومان", ""]
    if can_pay_from_balance:
        lines.append(f"💰 موجودی فعلی شما {account['balance']:,} تومان است - می‌توانید فوری از اعتبار پرداخت کنید،")
        lines.append("یا:")
        lines.append("")
    lines.append("مبلغ را به شماره کارت زیر واریز کنید و سپس عکس رسید را همینجا ارسال کنید:")
    lines.append("")
    if payment.get("payment_card_number"):
        lines.append(f"💳 <code>{payment['payment_card_number']}</code>")
    if payment.get("payment_card_holder"):
        lines.append(f"به نام: {payment['payment_card_holder']}")
    if payment.get("payment_instructions"):
        lines.append("\n" + payment["payment_instructions"])

    await state.set_state(CustomerPurchaseStates.waiting_receipt)
    await _reply(target, "\n".join(lines), receipt_choice_kb(can_pay_from_balance, final_price))


def _uid(target) -> int:
    return target.from_user.id


@router.callback_query(PayCB.filter(F.method == "balance"), CustomerPurchaseStates.waiting_receipt)
async def pay_with_balance(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Instant checkout using the customer's wallet balance instead of a
    card receipt - no admin approval needed, applied right away."""
    data = await state.get_data()
    pkg = data["packages"][data["package_id"]]
    kind = data["kind"]
    target_username = data.get("target_username")
    if not target_username:
        await call.answer("حساب شما پیدا نشد", show_alert=True)
        return

    add_gb = pkg.get("quota_gb") or 0
    add_days = pkg.get("duration_days") or 0
    discount_amount = data.get("discount_amount") or 0
    final_price = max(0, pkg["price"] - discount_amount)

    # Debit the wallet FIRST (atomic on the server side - see
    # routers/bot.py's add_balance) and only provision/renew after that
    # succeeds. This order matters: debiting last would let a customer keep
    # a service that was already provisioned even if the debit then failed
    # (e.g. a double-tap racing the balance down to insufficient funds in
    # between) - i.e. free service. Debiting first means the worst case on
    # a mid-flow failure is "charged but not yet provisioned", which we
    # recover from below with a refund instead of a free service. Charges
    # final_price (after any discount code applied in _advance_purchase_flow),
    # NOT the original pkg["price"].
    try:
        user = await api.add_balance(target_username, -final_price)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return

    # Consume the discount code now that payment actually succeeded - best
    # effort, since the customer has already been charged the discounted
    # amount either way; a failure here would only mean the code's
    # used_count/redemption record undercounts, not a wrong charge.
    if data.get("discount_code"):
        try:
            await api.redeem_discount(data["discount_code"], target_username, pkg["price"])
        except ApiError:
            pass

    # Collects only the connection(s) actually created by THIS purchase, so
    # the message below sends just those - not every connection the
    # customer has ever had (see api.get_user, which returns the FULL
    # history). Stays empty for a plain "renew" (nothing new is
    # provisioned, so nothing new to (re)send either - the old bug here was
    # resending every past service's config again on every renewal too).
    new_connections: list[dict] = []
    try:
        if kind == "new":
            # One batch per purchase, so if this package bundles more than
            # one service they group together under one "خرید" entry in
            # "اکانت من" instead of showing as separate unrelated services -
            # see models.Connection.purchase_batch.
            batch = uuid.uuid4().hex
            if data.get("node_id"):
                # plain package - single manually-picked service
                conn = await api.add_connection(
                    target_username, data["node_id"], data["protocol"], flow="",
                    purchase_batch=batch, package_name=pkg.get("name"),
                )
                new_connections.append(conn)
            else:
                # package with bundled services - provision all of them
                for c in (pkg.get("connections") or []):
                    conn = await api.add_connection(
                        target_username, c["node_id"], c["protocol"], flow=c.get("flow") or "",
                        purchase_batch=batch, package_name=pkg.get("name"),
                    )
                    new_connections.append(conn)
        if add_gb or add_days:
            await api.renew(target_username, add_gb=add_gb, add_days=add_days, package_id=pkg.get("id"))
        user = await api.get_user(target_username)
    except ApiError as exc:
        # Provisioning failed after the debit already went through - refund
        # so the customer isn't charged for nothing, and tell them clearly.
        try:
            await api.add_balance(target_username, final_price)
        except ApiError:
            pass
        await call.answer(f"خطا در فعال‌سازی سرویس - مبلغ به کیف پول شما بازگشت داده شد: {exc}", show_alert=True)
        return

    await state.clear()
    text = f"✅ خرید با موفقیت از اعتبار شما پرداخت شد.\n\nموجودی فعلی: {user['balance']:,} تومان"
    if user.get("reserved_quota_gb") or user.get("reserved_duration_days"):
        # renew_user() queued this instead of applying it right now - see
        # services/user_ops.py's renew_user docstring.
        text += "\n\n⏳ سرویس فعلی شما هنوز اعتبار دارد، پس این تمدید رزرو شد و به محض تمام شدنش خودکار فعال می‌شود."
    if user.get("loyalty_reward_credit") or user.get("loyalty_reward_gb"):
        text += "\n\n" + _loyalty_reward_text(user)
    await call.message.edit_text(text, reply_markup=home_kb())
    if new_connections:
        await send_connections(bot, call.from_user.id, new_connections)
    await send_package_extras(bot, call.from_user.id, pkg)
    try:
        await bot.send_message(call.from_user.id, "🏠 منو:", reply_markup=home_kb())
    except Exception:
        pass
    await call.answer("پرداخت شد")


@router.callback_query(PackageCB.filter(), CustomerPurchaseStates.picking_package)
async def pick_package(call: CallbackQuery, callback_data: PackageCB, state: FSMContext) -> None:
    data = await state.get_data()
    pkg = data["packages"].get(callback_data.package_id) or data["packages"].get(str(callback_data.package_id))
    if not pkg:
        await call.answer("پکیج پیدا نشد", show_alert=True)
        return
    await state.update_data(package_id=pkg["id"])

    if callback_data.kind == "renew":
        await _ask_for_receipt(call, state)
        return

    # If the package already bundles specific server+protocol combos
    # (set up by the admin in "پکیج‌ها"), use those automatically instead
    # of asking the customer to pick one - otherwise only the single
    # manually-picked service ever got provisioned, even for packages
    # meant to hand out several at once.
    if pkg.get("connections"):
        await _ask_for_receipt(call, state)
        return

    # plain package with no bundled services still needs a node + protocol
    # picked manually, same as before
    try:
        nodes = await api.list_nodes()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not nodes:
        await call.message.edit_text("در حال حاضر سروری فعال نیست.", reply_markup=home_kb())
        await call.answer()
        return
    await state.update_data(nodes={n["id"]: n for n in nodes})
    await state.set_state(CustomerPurchaseStates.picking_node)
    await call.message.edit_text("کدام سرویس را می‌خواهید؟", reply_markup=nodes_kb(nodes))
    await call.answer()


@router.callback_query(NodeCB.filter(), CustomerPurchaseStates.picking_node)
async def pick_node(call: CallbackQuery, callback_data: NodeCB, state: FSMContext) -> None:
    data = await state.get_data()
    node = data["nodes"].get(callback_data.node_id) or data["nodes"].get(str(callback_data.node_id))
    if not node:
        await call.answer("سرور پیدا نشد", show_alert=True)
        return
    await state.update_data(node_id=node["id"], node_name=node["name"])
    await state.set_state(CustomerPurchaseStates.picking_protocol)
    await call.message.edit_text("نوع اتصال را انتخاب کنید:", reply_markup=protocols_kb(node["type"]))
    await call.answer()


@router.callback_query(ProtocolCB.filter(), CustomerPurchaseStates.picking_protocol)
async def pick_protocol(call: CallbackQuery, callback_data: ProtocolCB, state: FSMContext) -> None:
    await state.update_data(protocol=callback_data.protocol)
    await _ask_for_receipt(call, state)


@router.message(CustomerPurchaseStates.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    pkg = data["packages"][data["package_id"]]
    kind = data["kind"]
    target_username = data.get("target_username") or f"tg{message.from_user.id}"

    discount_amount = data.get("discount_amount") or 0
    request_id = storage.create_pending(
        telegram_id=message.from_user.id,
        telegram_username=message.from_user.username,
        telegram_name=message.from_user.full_name,
        kind=kind,
        package=pkg,
        target_username=target_username,
        node_id=data.get("node_id"),
        node_name=data.get("node_name"),
        protocol=data.get("protocol"),
        receipt_file_id=message.photo[-1].file_id,
        referral_code=data.get("referral_code"),
        discount_code=data.get("discount_code"),
        discount_amount=discount_amount,
        final_price=max(0, pkg.get("price", 0) - discount_amount),
    )
    await state.clear()
    await message.answer("✅ رسید شما ثبت شد و برای بررسی ادمین ارسال شد. نتیجه به همین چت اطلاع داده می‌شود.", reply_markup=home_kb())

    from .admin_pending import _pending_summary  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    caption = "🧾 رسید پرداخت جدید\n\n" + _pending_summary(storage.get_pending(request_id))
    for admin_id in config.approval_targets():
        try:
            await bot.send_photo(admin_id, message.photo[-1].file_id, caption=caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass


@router.message(CustomerPurchaseStates.waiting_receipt)
async def receive_receipt_wrong_type(message: Message) -> None:
    await message.answer("لطفا عکس رسید پرداخت را ارسال کنید (نه متن).", reply_markup=cancel_kb())


# ------------------------------------------------------------- top up credit
async def _ask_for_topup_receipt(target, state: FSMContext, amount: int) -> None:
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        if isinstance(target, CallbackQuery):
            await target.answer(f"خطا: {exc}", show_alert=True)
        else:
            await target.answer(f"خطا: {exc}")
        return
    await state.update_data(topup_amount=amount)
    lines = [
        f"مبلغ افزایش اعتبار: <b>{amount:,} تومان</b>",
        "",
        "لطفا مبلغ را به شماره کارت زیر واریز کنید و سپس عکس رسید را همینجا ارسال کنید:",
        "",
    ]
    if payment.get("payment_card_number"):
        lines.append(f"💳 <code>{payment['payment_card_number']}</code>")
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
    )
    await state.clear()
    await message.answer("✅ رسید شما ثبت شد و برای بررسی ادمین ارسال شد. نتیجه به همین چت اطلاع داده می‌شود.", reply_markup=home_kb())

    from .admin_pending import _pending_summary  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    caption = "🧾 رسید افزایش اعتبار\n\n" + _pending_summary(storage.get_pending(request_id))
    for admin_id in config.approval_targets():
        try:
            await bot.send_photo(admin_id, message.photo[-1].file_id, caption=caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass


@router.message(CustomerTopupStates.waiting_receipt)
async def receive_topup_receipt_wrong_type(message: Message) -> None:
    await message.answer("لطفا عکس رسید پرداخت را ارسال کنید (نه متن).", reply_markup=cancel_kb())
