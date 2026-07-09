import uuid

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..callbacks import MenuCB, PackageCB, NodeCB, ProtocolCB, TopupAmountCB, PayCB, ConnectionCB, PurchaseCB
from ..config import config
from ..admin_scope import resolve_admin_scope
from ..keyboards import (
    packages_kb,
    nodes_kb,
    protocols_kb,
    cancel_kb,
    home_kb,
    main_menu_kb,
    topup_amounts_kb,
    receipt_choice_kb,
    connections_list_kb,
    group_connections_by_purchase,
    purchases_kb,
    usage_per_service_text,
    standalone_usage_text,
)
from ..states import CustomerLinkStates, CustomerPurchaseStates, CustomerTopupStates
from ..utils import fmt_bytes, fmt_date, STATUS_LABELS
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
        f"انقضا: {fmt_date(user.get('expire_at'))}",
        f"موجودی اعتبار: {user.get('balance', 0):,} تومان",
    ]
    if user["connections"]:
        lines.append(f"\n<b>خریدهای شما ({len(user['connections'])} سرویس):</b> روی هرکدوم از دکمه‌های پایین بزنید 👇")
        lines.append(usage_per_service_text(user["connections"]))
    return "\n".join(lines)


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
    await state.clear()
    try:
        user = await api.get_user_by_telegram(message.from_user.id)
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
        return
    if not user:
        scope = await resolve_admin_scope(message.from_user.id)
        await message.answer("هنوز حسابی برای شما ثبت نشده.", reply_markup=main_menu_kb(scope))
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
    await state.set_state(CustomerPurchaseStates.picking_package)
    await state.update_data(kind="new", packages={p["id"]: p for p in packages})
    await message.answer("یک پکیج انتخاب کنید:", reply_markup=packages_kb(packages, "new"))


@router.message(Command("topup"))
async def cmd_topup(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "💰 افزایش اعتبار"."""
    await state.clear()
    try:
        account = await api.get_user_by_telegram(message.from_user.id)
    except ApiError as exc:
        await message.answer(f"خطا: {exc}")
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
    await state.clear()
    try:
        user = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not user:
        scope = await resolve_admin_scope(call.from_user.id)
        await call.message.edit_text(
            "هنوز حسابی برای شما ثبت نشده.", reply_markup=main_menu_kb(scope)
        )
        await call.answer()
        return
    groups = group_connections_by_purchase(user["connections"]) if user["connections"] else []
    await call.message.edit_text(
        _account_text(user),
        reply_markup=purchases_kb(groups) if groups else home_kb(),
    )
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_usage"))
async def cb_usage(call: CallbackQuery, bot: Bot) -> None:
    """Dedicated top-level "📊 مصرف سرویس‌ها" button - shows the same
    per-service usage breakdown as the section under "اکانت من"
    (usage_per_service_text), but as its own standalone view reachable in
    one tap instead of having to open the account view first."""
    try:
        user = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not user:
        await call.message.edit_text("هنوز حسابی برای شما ثبت نشده.", reply_markup=home_kb())
        await call.answer()
        return
    await call.message.edit_text(standalone_usage_text(user["connections"]), reply_markup=home_kb())
    await call.answer()


@router.callback_query(PurchaseCB.filter())
async def cb_view_purchase(call: CallbackQuery, callback_data: PurchaseCB, bot: Bot) -> None:
    """Fires when a customer taps one multi-service purchase button under
    "👤 اکانت من" - opens a submenu listing just that purchase's services
    (single-service purchases skip this entirely - purchases_kb wires their
    button straight to ConnectionCB, same as before this feature existed)."""
    try:
        user = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
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
async def cb_view_connection(call: CallbackQuery, callback_data: ConnectionCB, bot: Bot) -> None:
    """Fires when a customer taps one specific service button under "👤
    اکانت من" - sends just that service's config/link/QR, instead of the
    old behavior of dumping every service automatically the moment the
    account view opened."""
    try:
        user = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
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


# --------------------------------------------------------------- link existing
@router.callback_query(MenuCB.filter(F.action == "cust_link"))
async def cb_link_start(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CustomerLinkStates.waiting_username)
    await call.message.edit_text("نام کاربری حساب قبلی‌تان را بفرستید:", reply_markup=cancel_kb())
    await call.answer()


@router.message(CustomerLinkStates.waiting_username)
async def link_username(message: Message, state: FSMContext, bot: Bot) -> None:
    username = (message.text or "").strip()
    try:
        await api.get_user(username)
        user = await api.link_telegram(username, message.from_user.id)
    except ApiError as exc:
        await message.answer(f"خطا: {exc}\nدوباره امتحان کنید یا انصراف بدهید:", reply_markup=cancel_kb())
        return
    await state.clear()
    await message.answer("✅ حساب شما وصل شد:\n\n" + _account_text(user), reply_markup=home_kb())
    if user["connections"]:
        await send_connections(bot, message.from_user.id, user["connections"])
        try:
            await bot.send_message(message.from_user.id, "🏠 منو:", reply_markup=home_kb())
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
    await state.set_state(CustomerPurchaseStates.picking_package)
    await state.update_data(kind=kind, packages={p["id"]: p for p in packages})
    await call.message.edit_text("یک پکیج انتخاب کنید:", reply_markup=packages_kb(packages, kind))
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_buy"))
async def cb_buy(call: CallbackQuery, state: FSMContext) -> None:
    await _start_package_picker(call, state, "new")


@router.callback_query(MenuCB.filter(F.action == "cust_renew"))
async def cb_renew(call: CallbackQuery, state: FSMContext) -> None:
    try:
        account = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    if not account:
        await call.answer("ابتدا باید یک حساب داشته باشید یا حساب قبلی را وصل کنید.", show_alert=True)
        return
    await state.update_data(target_username=account["username"])
    await _start_package_picker(call, state, "renew")


async def _ask_for_receipt(call: CallbackQuery, state: FSMContext) -> None:
    try:
        payment = await api.get_payment_info()
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return
    data = await state.get_data()
    pkg = data["packages"][data["package_id"]]
    price = pkg["price"]

    # If the customer already has a linked account with enough balance,
    # offer an instant "pay from balance" option alongside the usual
    # card-to-card receipt flow (see pay_with_balance below).
    account = None
    try:
        account = await api.get_user_by_telegram(call.from_user.id)
    except ApiError:
        account = None
    can_pay_from_balance = bool(account and price and (account.get("balance") or 0) >= price)
    if account:
        await state.update_data(target_username=account["username"])

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
    await call.message.edit_text("\n".join(lines), reply_markup=receipt_choice_kb(can_pay_from_balance, price))
    await call.answer()


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

    # Debit the wallet FIRST (atomic on the server side - see
    # routers/bot.py's add_balance) and only provision/renew after that
    # succeeds. This order matters: debiting last would let a customer keep
    # a service that was already provisioned even if the debit then failed
    # (e.g. a double-tap racing the balance down to insufficient funds in
    # between) - i.e. free service. Debiting first means the worst case on
    # a mid-flow failure is "charged but not yet provisioned", which we
    # recover from below with a refund instead of a free service.
    try:
        user = await api.add_balance(target_username, -pkg["price"])
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
        return

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
            await api.renew(target_username, add_gb=add_gb, add_days=add_days)
        user = await api.get_user(target_username)
    except ApiError as exc:
        # Provisioning failed after the debit already went through - refund
        # so the customer isn't charged for nothing, and tell them clearly.
        try:
            await api.add_balance(target_username, pkg["price"])
        except ApiError:
            pass
        await call.answer(f"خطا در فعال‌سازی سرویس - مبلغ به کیف پول شما بازگشت داده شد: {exc}", show_alert=True)
        return

    await state.clear()
    text = f"✅ خرید با موفقیت از اعتبار شما پرداخت شد.\n\nموجودی فعلی: {user['balance']:,} تومان"
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
    try:
        account = await api.get_user_by_telegram(call.from_user.id)
    except ApiError as exc:
        await call.answer(f"خطا: {exc}", show_alert=True)
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
