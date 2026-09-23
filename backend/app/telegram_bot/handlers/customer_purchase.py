"""New-purchase and renewal flow - package/node/protocol picking, the
referral/discount/comment pre-payment steps, the payment screen (card
receipt or instant balance pay), and the free-package (trial) shortcut.
Split out of the old single customer.py (see customer_common.py's module
docstring for why).

Note: the "🛒 خرید اکانت جدید" SLASH COMMAND (cmd_buy) is not here - it lives
on customer.py's own router alongside the other three slash commands, so it
is always checked before any catch-all state handler regardless of which
section file that catch-all lives in. See customer.py's module docstring.
_distinct_session_counts is defined here (not customer.py) since cmd_buy is
its only facade-side caller and this file is where the rest of its callers
live - customer.py imports it from here."""
import logging

from aiogram import Router, F, Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..callbacks import MenuCB, NodeCB, PackageCB, PayCB, ProtocolCB, RenewServiceCB, SessionCountCB
from ..connection_sender import send_connections
from ..panel_bridge import api, ApiError
from ..keyboards import (
    cancel_kb,
    home_kb,
    nodes_kb,
    packages_kb,
    promo_skip_kb,
    protocols_kb,
    receipt_choice_kb,
    session_count_kb,
)
from ..states import CustomerPurchaseStates
from ..utils import packages_message
from .. import storage
from .customer_common import (
    NO_PAYMENT_METHOD,
    _blocked_from_buying,
    _clear_state_keep_account,
    _menu_item_enabled,
    _notify_targets,
    _reply_menu_item_disabled,
    _resolve_account,
    _resolve_account_silent,
    _send_menu_footer,
    send_package_extras,
)

logger = logging.getLogger("telegram_bot")

router = Router(name="customer_purchase")


def _distinct_session_counts(packages: list[dict]) -> list[int]:
    """Every distinct Package.max_concurrent_sessions value present among
    the given packages, sorted ascending (0 stands in for None/unlimited).
    Used to decide whether the "چند کاربره می‌خواهید؟" step is worth
    showing at all - if every available package shares the same limit (the
    common case for an admin selling only single-user packages, say),
    skipping straight to the package list avoids an extra, pointless tap."""
    return sorted({(p.get("max_concurrent_sessions") or 0) for p in packages})


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


def _sale_info(data: dict, final_price: int, method: str) -> dict:
    """Exact-amount accounting details passed through to routers/bot.py's
    ledger hook (see services/accounting.py) - the bot is the only place
    that knows the post-discount price the customer really paid and which
    card their payment screen showed, so it sends them along instead of
    letting the panel fall back to the package's list price."""
    return {
        "paid_amount": final_price,
        "payment_method": method,
        "payment_card_id": data.get("payment_card_id") if method == "card" else None,
        "discount_code": data.get("discount_code"),
        "discount_amount": data.get("discount_amount") or None,
    }


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
    await call.message.edit_text(packages_message(packages), reply_markup=packages_kb(packages, kind))
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
    await call.message.edit_text(packages_message(filtered), reply_markup=packages_kb(filtered, callback_data.kind))
    await call.answer()


@router.callback_query(MenuCB.filter(F.action == "cust_buy"))
async def cb_buy(call: CallbackQuery, state: FSMContext) -> None:
    # Unlike cmd_buy (the /buy slash command, which does a full state.clear()),
    # this button is the ~99%-used entry point and can be tapped from an old
    # still-live "🏠 خانه"/menu message sitting in the chat - without
    # clearing first, a stale discount_code/discount_step_done/
    # referral_step_done left over from a previous ABANDONED purchase
    # attempt (started, a code applied, then never finished) silently
    # carries into this new one: _advance_purchase_flow's "already done"
    # guards skip straight past the referral/discount steps and reuse the
    # old discount_amount/code - computed against a DIFFERENT package's
    # price - as this purchase's final_price, and redeem_discount can end
    # up called again for a single-use code. _clear_state_keep_account
    # (not a full state.clear()) so the multi-account picker memory
    # (active_username) survives, same as every other button entry point.
    if not await _menu_item_enabled("cust_buy"):
        await _reply_menu_item_disabled(call)
        return
    if await _blocked_from_buying(call, call.from_user.id):
        return
    await _clear_state_keep_account(state)
    await _start_package_picker(call, state, "new")


@router.callback_query(MenuCB.filter(F.action == "cust_renew"))
async def cb_renew(call: CallbackQuery, state: FSMContext) -> None:
    # Same stale-state gap as cb_buy above, same fix.
    if not await _menu_item_enabled("cust_renew"):
        await _reply_menu_item_disabled(call)
        return
    if await _blocked_from_buying(call, call.from_user.id):
        return
    await _clear_state_keep_account(state)
    account = await _resolve_account(call, state, call.from_user.id, "cust_renew")
    if account == "ambiguous":
        return
    if not account:
        await call.answer("ابتدا باید یک حساب داشته باشید یا حساب قبلی را وصل کنید.", show_alert=True)
        return
    await state.update_data(target_username=account["username"])

    # تمدید = ادامه‌ی همان سرویس قبلی (same connections/credentials, new
    # package queued behind what's left - see routers/bot.py's
    # renew_service). If the customer has several independent services,
    # they pick WHICH one they're renewing before seeing the package list;
    # with exactly one it's auto-targeted; with none (pre-migration edge
    # case) the old user-level renew still applies server-side.
    try:
        purchases = await api.list_purchases(account["username"])
    except ApiError:
        purchases = []
    if len(purchases) >= 2:
        await state.update_data(renew_purchases={str(p["id"]): p for p in purchases})
        await state.set_state(CustomerPurchaseStates.picking_service)
        rows = []
        for p in purchases:
            name = p.get("package_name_snapshot") or "سرویس"
            if p.get("quota_bytes"):
                remaining = max(0, (p["quota_bytes"] - (p.get("used_bytes") or 0))) / (1024 ** 3)
                detail = f"{remaining:.1f} GB مانده"
            else:
                detail = "نامحدود"
            rows.append([InlineKeyboardButton(
                text=f"{name} · {detail}",
                callback_data=RenewServiceCB(purchase_id=p["id"]).pack(),
            )])
        await call.message.edit_text(
            "کدام سرویس را می‌خواهید تمدید کنید؟",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )
        await call.answer()
        return
    if len(purchases) == 1:
        await state.update_data(renew_purchase_id=purchases[0]["id"])
    await _start_package_picker(call, state, "renew")


@router.callback_query(RenewServiceCB.filter(), CustomerPurchaseStates.picking_service)
async def pick_renew_service(call: CallbackQuery, callback_data: RenewServiceCB, state: FSMContext) -> None:
    data = await state.get_data()
    p = (data.get("renew_purchases") or {}).get(str(callback_data.purchase_id))
    if not p:
        await call.answer("سرویس پیدا نشد", show_alert=True)
        return
    await state.update_data(renew_purchase_id=callback_data.purchase_id)
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

    # 3) optional free-form label for THIS service (models.Purchase.comment)
    # - only for a NEW purchase (a renewal continues an existing service
    # that already has, or doesn't need, its own label). Shown next to the
    # service on the customer's subscription page, so someone buying
    # several services can tell them apart.
    if data.get("kind") == "new" and not data.get("comment_step_done"):
        await state.update_data(comment_step_done=True)
        await state.set_state(CustomerPurchaseStates.entering_comment)
        await _reply(
            target,
            "📝 می‌خواهید یک نام برای این سرویس بگذارید؟ (مثلا: گوشی خودم، لپ‌تاپ کار)\n\n"
            "همینجا تایپ کنید یا دکمه رد کردن را بزنید.",
            promo_skip_kb(),
        )
        return

    await _show_payment_screen(target, state)


@router.callback_query(MenuCB.filter(F.action == "promo_skip"), CustomerPurchaseStates.entering_comment)
async def skip_comment(call: CallbackQuery, state: FSMContext) -> None:
    await _advance_purchase_flow(call, state)


@router.message(CustomerPurchaseStates.entering_comment, F.text)
async def enter_comment(message: Message, state: FSMContext) -> None:
    await state.update_data(comment=message.text.strip()[:255])
    await _advance_purchase_flow(message, state)


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


async def _give_free_package(target, state: FSMContext, pkg: dict, account) -> None:
    """A package that costs nothing, handed over on the spot.

    Writes the same pending row a receipt would (minus the receipt - there
    is no payment to evidence) and then approves it immediately, so a free
    package travels the identical route a paid one does after an admin
    presses Approve. The alternative - provisioning inline here - would be
    a second implementation of account creation, referral redemption,
    config delivery and notification, free to drift from the first.
    """
    from .admin_pending import perform_approval

    data = await state.get_data()
    target_username = data.get("target_username") or (account or {}).get("username")
    kind = data.get("kind", "new")
    if not target_username:
        # Brand-new customer: the same tgNNN placeholder the receipt flow
        # uses. perform_approval creates the account under it.
        target_username = f"tg{_uid(target)}"

    request_id = storage.create_pending(
        telegram_id=_uid(target),
        telegram_username=getattr(target.from_user, "username", None),
        telegram_name=getattr(target.from_user, "full_name", None),
        kind=kind,
        package=pkg,
        target_username=target_username,
        node_id=data.get("node_id"),
        node_name=data.get("node_name"),
        protocol=data.get("protocol"),
        referral_code=data.get("referral_code"),
        final_price=0,
        renew_purchase_id=data.get("renew_purchase_id"),
        comment=data.get("comment"),
    )
    await state.clear()

    pending = storage.get_pending(request_id)
    bot = target.bot if hasattr(target, "bot") else None
    ok, message = await perform_approval(pending, bot)
    if not ok:
        # The refusals here are the trial's own rules (already taken, not a
        # new customer, daily cap) - the customer's own answer, so it is
        # shown rather than logged.
        storage.set_status(request_id, "rejected")
    await _reply(target, message or ("✅ سرویس شما فعال شد." if ok else "انجام نشد."), home_kb())


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
    #
    # Prefer a target_username already resolved earlier THIS purchase (by
    # cb_renew, which resolves it eagerly, or by _ask_for_receipt's own
    # silent resolve right after package/protocol pick) over re-running
    # _resolve_account_silent's telegram_id lookup here from scratch. For a
    # customer with 2+ linked accounts (bought more than once - see
    # User.telegram_id), that lookup only succeeds via `active_username` in
    # FSM state - but this bot's Dispatcher uses aiogram's MemoryStorage
    # (see runner.py), which is wiped on every bot restart/redeploy. A
    # multi-account customer who picked an account earlier in the exact
    # same session (or even a few messages ago, if the bot happened to
    # restart in between) would silently lose that pick here and get
    # nothing back from _resolve_account_silent even though which account
    # this purchase is for was never actually in doubt - the "پرداخت از
    # طریق اعتبار" button would then just vanish despite real, sufficient
    # balance. Fetching the already-known username directly sidesteps the
    # ambiguity check entirely.
    target_username = data.get("target_username")
    if target_username:
        try:
            account = await api.get_user(target_username)
        except ApiError:
            account = None
    else:
        account = await _resolve_account_silent(state, _uid(target))
    can_pay_from_balance = bool(account and final_price and (account.get("balance") or 0) >= final_price)
    if account:
        await state.update_data(target_username=account["username"])

    # Nothing to pay means nothing to ask. A free package (the trial - see
    # backend models.Package.is_trial) used to land in the card-to-card
    # branch below, because `final_price` of 0 is falsy and so
    # can_pay_from_balance was False - so the bot showed a card number and
    # asked for a receipt for a zero-toman package. Reported 2026-09-15:
    # «برای تست رایگان گزینه پرداخت نباید بیاد چه بات چه مینی اپ».
    #
    # Handed straight to the approval path instead of being provisioned
    # here by hand. That path already knows how to create a brand-new
    # account, apply a referral code, send the configs and notify everyone
    # - all of which a free package needs exactly as much as a paid one,
    # and none of which is worth a second copy of.
    if final_price <= 0:
        await _give_free_package(target, state, pkg, account)
        return

    if discount_amount:
        lines = [f"پکیج: <b>{pkg['name']}</b> — <s>{price:,}</s> {final_price:,} تومان (🎟 {discount_amount:,} تومان تخفیف)", ""]
    else:
        lines = [f"پکیج: <b>{pkg['name']}</b> — {price:,} تومان", ""]
    if can_pay_from_balance:
        lines.append(f"💰 موجودی فعلی شما {account['balance']:,} تومان است - می‌توانید فوری از اعتبار پرداخت کنید،")
        lines.append("یا:")
        lines.append("")
    if payment.get("payment_card_number"):
        lines.append("مبلغ را به شماره کارت زیر واریز کنید و سپس عکس رسید را همینجا ارسال کنید:")
        lines.append("")
        lines.append(f"💳 <code>{payment['payment_card_number']}</code>")
        if payment.get("payment_card_holder"):
            lines.append(f"به نام: {payment['payment_card_holder']}")
        if payment.get("payment_instructions"):
            lines.append("\n" + payment["payment_instructions"])
    else:
        lines.append(NO_PAYMENT_METHOD)
        if not can_pay_from_balance:
            # Nothing to wait for: no card to pay into and no balance to
            # spend. Sending them into the receipt state here would leave
            # them stuck on "send me the photo" with no way to produce one.
            await state.clear()
            await _reply(target, "\n".join(lines), home_kb())
            return

    # Remembered so receive_receipt below can stamp it onto the pending
    # request - see storage.py's payment_card_id column docstring for why
    # (threshold-mode bookkeeping once an admin approves this purchase).
    await state.update_data(payment_card_id=payment.get("resolved_payment_card_id"))

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
            # target_username here is ALWAYS an already-existing, already-
            # linked account (see the check at the top of this function -
            # paying from balance requires an account with sufficient
            # balance in the first place, so there is no "brand new
            # customer" case to handle here, unlike admin_pending.py's
            # receipt-approval path). Gives them a brand-new, independently
            # -enforced Purchase (own quota_bytes/expire_at) instead of the
            # old add_connection()+renew() pairing, which pooled this
            # purchase's quota/duration into the customer's SHARED total -
            # see user_ops.apply_package_as_purchase's docstring. That old
            # behavior is the exact bug report this fixes: buying a SECOND
            # service looked, from the customer's side, exactly like the
            # FIRST service had just been renewed (later expiry, no visibly
            # separate new service) - because that's genuinely what it was
            # doing under the hood.
            connections_override = None
            if data.get("node_id"):
                # plain package - single manually-picked service
                connections_override = [{"node_id": data["node_id"], "protocol": data["protocol"], "flow": ""}]
            # else: bundled package - purchase_package uses the package's
            # OWN connections list server-side (fetched fresh from the DB,
            # not this possibly-stale bot-side pkg snapshot).
            result = await api.purchase_package(
                target_username, pkg["id"], connections=connections_override,
                sale_info=_sale_info(data, final_price, "wallet"),
                comment=data.get("comment"),
            )
            new_connections = result["connections"]
        elif add_gb or add_days:
            # تمدید = ادامه‌ی همان سرویس انتخاب‌شده (renew_purchase_id از
            # مرحله «کدام سرویس؟») - fallback to the user-level endpoint
            # only when no specific service was picked, where the server
            # itself auto-targets a single-service customer.
            if data.get("renew_purchase_id"):
                await api.renew_service(
                    target_username, data["renew_purchase_id"],
                    add_gb=add_gb, add_days=add_days, package_id=pkg.get("id"),
                    sale_info=_sale_info(data, final_price, "wallet"),
                )
            else:
                await api.renew(
                    target_username, add_gb=add_gb, add_days=add_days, package_id=pkg.get("id"),
                    sale_info=_sale_info(data, final_price, "wallet"),
                )
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
    await _send_menu_footer(bot, call.from_user.id)
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
        payment_card_id=data.get("payment_card_id"),
        renew_purchase_id=data.get("renew_purchase_id"),
        comment=data.get("comment"),
    )
    await state.clear()
    await message.answer("✅ رسید شما ثبت شد و برای بررسی ادمین ارسال شد. نتیجه به همین چت اطلاع داده می‌شود.", reply_markup=home_kb())

    from .admin_pending import _pending_summary, _owner_label  # local import avoids a circular import at module load
    from ..keyboards import approval_kb

    pending_row = storage.get_pending(request_id)

    # Auto-approval (see services/auto_approve.py) is attempted BEFORE the
    # admins are notified, so a qualifying request never produces an approval
    # prompt that is already stale by the time anyone opens Telegram. It
    # fails closed - anything it declines simply falls through to the normal
    # notification below.
    from ...services import auto_approve

    auto_note = ""
    try:
        approved, reason = await auto_approve.try_auto_approve(pending_row, bot)
        if approved:
            return
        auto_note = reason
    except Exception:
        logger.exception("auto-approve raised - falling back to manual approval")
        auto_note = "بررسی تایید خودکار با خطا مواجه شد"

    caption = "🧾 رسید پرداخت جدید\n\n" + _pending_summary(pending_row, await _owner_label(pending_row.get("owner_admin_id")))
    # Says WHY this one still needs a human. Without it, an owner who has
    # switched auto-approval on sees an ordinary approval prompt and can
    # only conclude the feature is broken - the reason was written to a log
    # inside a container, which in practice means written nowhere.
    if auto_note:
        caption += f"\n\n🤖 تایید خودکار انجام نشد: {auto_note}"
    for admin_id in await _notify_targets(pending_row):
        try:
            await bot.send_photo(admin_id, message.photo[-1].file_id, caption=caption, reply_markup=approval_kb(request_id))
        except Exception:
            pass


@router.message(CustomerPurchaseStates.waiting_receipt)
async def receive_receipt_wrong_type(message: Message) -> None:
    await message.answer("لطفا عکس رسید پرداخت را ارسال کنید (نه متن).", reply_markup=cancel_kb())
