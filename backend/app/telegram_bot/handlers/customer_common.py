"""Shared helpers behind every customer-facing flow - split out of what used
to be one 1552-line customer.py (graphify flagged its low internal cohesion:
0.07, see GRAPH_REPORT.md) into this foundation module plus customer_account.py,
customer_link.py, customer_purchase.py, and customer_topup.py, one per
self-contained flow. This module deliberately imports none of those four -
it is the shared base they all sit on, never a peer, so it can never end up
in an import cycle with them. See customer.py for how the pieces are wired
back together (it also owns the /account, /link, /buy, /topup slash
commands - see its module docstring for why those specifically could not
move into the section files below).

Everything here is customer-facing - admins can use it too (no harm), but
the "no account yet" messaging is written for customers."""
import logging

from aiogram import Bot
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from ..panel_bridge import api, ApiError
from ..config import config
from ..keyboards import account_picker_kb, home_kb, main_menu_kb

logger = logging.getLogger("telegram_bot")

# Shown instead of a card number when the reseller has not set one.
#
# routers/bot.py's get_payment_info no longer falls back to the MAIN
# admin's card for a reseller who has not configured their own - a customer
# paying into the wrong person's account is worse than a customer who
# cannot pay yet. This is what they see instead, and the screens below must
# not ask for a receipt when it is showing: "pay into nothing, then send me
# the proof" is a dead end the customer cannot get out of.
NO_PAYMENT_METHOD = "هنوز روش پرداخت تنظیم نشده - با پشتیبانی تماس بگیرید."


async def _notify_targets(pending_row: dict) -> set:
    """Who should be told about this pending receipt.

    config.approval_targets() only reflects whichever bot instance's
    thread actually received the message - the shared bot's static
    admin_ids unless the customer happened to be talking to the owning
    reseller's own dedicated bot AND it is correctly linked. Reported
    2026-09-06: a reseller's customer paying through "the reseller's own
    bot" produced a receipt that reached the main/shared admin instead of
    the reseller. Whatever the exact routing accident, the pending row's
    own owner_admin_id (see storage.py) always names the real owner - so
    that owner's linked Telegram id is added here unconditionally,
    ensuring they get a copy even when approval_targets() resolves to
    someone else. Falls back to approval_targets() alone (unchanged
    behaviour) when the owner has no linked id or the request is
    ownerless (shared-panel customer).

    Also adds the specific PAYMENT CARD's own approval_telegram_id (see
    models.PaymentCard), if the admin set one for it - lets different
    cards be watched by different people (e.g. each card belongs to
    someone who only wants to see receipts paid to their own card) on top
    of, never instead of, the targets above. Looked up by the exact
    payment_card_id recorded on this request at payment-screen time, not
    whichever card the pool currently considers active - the pool may
    have rotated to a different card since."""
    targets = set(config.approval_targets())
    owner_admin_id = pending_row.get("owner_admin_id") if pending_row else None
    if owner_admin_id:
        owner_tg = await api.get_admin_telegram_id(owner_admin_id)
        if owner_tg:
            targets.add(owner_tg)
    payment_card_id = pending_row.get("payment_card_id") if pending_row else None
    if payment_card_id:
        card = await api.get_payment_card(payment_card_id)
        if card and card.get("approval_telegram_id"):
            targets.add(card["approval_telegram_id"])
    return targets


async def _send_menu_footer(bot: Bot, chat_id: int) -> None:
    """Re-posts the main menu AFTER a batch of config/document messages.
    Those are sent as fresh messages, which pushes the previous menu out of
    view - without this the customer is left staring at the last config
    with the menu stranded somewhere above it."""
    try:
        await bot.send_message(chat_id, "🏠 منو:", reply_markup=await main_menu_kb(None))
    except Exception:
        pass


async def _menu_item_enabled(action: str) -> bool:
    """True unless the admin has hidden this item from Settings > ربات >
    منوی مشتری (models.BotSettings.customer_menu_disabled_items).
    keyboards.main_menu_kb() already skips drawing a button for a disabled
    item, but that alone doesn't stop a customer who already has the
    button in an older chat message, or who just types the matching slash
    command (/account, /link, /buy, /topup - see runner.py's
    CUSTOMER_COMMANDS, which is NOT filtered by this setting) from
    reaching the flow anyway. Call this at the top of every entry point a
    toggleable CUSTOMER_MENU_ITEMS action maps to, so disabling it in
    Settings actually blocks the feature instead of just hiding its
    button. Fails OPEN (returns True) on an ApiError - get_customer_menu_
    disabled_items_cached already swallows that into an empty list, which
    reads as "nothing disabled" here, same net effect."""
    from ..panel_bridge import get_customer_menu_disabled_items_cached

    disabled = await get_customer_menu_disabled_items_cached()
    return action not in disabled


async def _reply_menu_item_disabled(target) -> None:
    text = "این قابلیت در حال حاضر توسط ادمین غیرفعال شده است."
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text, reply_markup=home_kb())


DEFAULT_PURCHASE_BLOCK_TEXT = (
    "امکان خرید و تمدید برای این حساب فعلا غیرفعال است.\n"
    "سرویس فعلی شما تا پایان اعتبارش کار می‌کند.\n"
    "برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
)


async def _purchase_block_reason(tg_id: int) -> str | None:
    """The admin's own words for why this customer may no longer buy, or
    None if they may ("قفل خرید" - models.User.purchases_blocked).

    Checked against EVERY account on this Telegram id, not just the active
    one: the lock is on the person, so switching accounts in the picker
    must not switch it off. The backend enforces the same rule (see
    routers/bot.py's _ensure_can_buy/_ensure_telegram_can_buy) - this only
    gets the customer a real explanation instead of a button that fails.

    Fails OPEN on an ApiError, exactly like _menu_item_enabled: a panel
    hiccup should not lock out customers who are not actually locked. The
    backend still refuses if they really are.
    """
    try:
        accounts = await api.list_users_by_telegram(tg_id)
    except ApiError:
        return None
    for account in accounts or []:
        if account.get("purchases_blocked"):
            return (account.get("purchases_blocked_reason") or "").strip() or DEFAULT_PURCHASE_BLOCK_TEXT
    return None


async def _blocked_from_buying(target, tg_id: int) -> bool:
    """True (and the customer has been told why) if the till is closed."""
    reason = await _purchase_block_reason(tg_id)
    if reason is None:
        return False
    if isinstance(target, CallbackQuery):
        # show_alert pops it up rather than flashing it away - this is not
        # a "try again" error, it needs reading.
        await target.answer(reason, show_alert=True)
    else:
        await target.answer(reason, reply_markup=home_kb())
    return True


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
