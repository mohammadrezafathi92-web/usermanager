"""Customer-facing entry point - the router that handlers/__init__.py's
build_router() actually includes for the customer side of the bot.

This used to be one 1552-line file (94 functions - graphify flagged it as
the single lowest-cohesion module in the whole bot, community cohesion score
0.07, see graphify-out/GRAPH_REPORT.md's "Suggested Questions"). It is now
split by flow into customer_common.py (shared helpers, no sibling imports),
customer_account.py ("👤 اکانت من" and the rest of the account-viewing menu),
customer_link.py ("🔗 وصل کردن حساب قبلی"), customer_purchase.py ("🛒 خرید"/
"🔄 تمدید"), and customer_topup.py ("💰 افزایش اعتبار") - this file wires
those four into one router and re-exports the handful of symbols other
modules (tutorials.py, persistent_menu.py, admin_pending.py,
services/auto_approve.py, and several tests) reach into `customer` for.

Why the four slash commands (cmd_account/cmd_link/cmd_buy/cmd_topup) stay
HERE instead of moving into their matching section file: aiogram's Router
checks its OWN directly-registered handlers before it ever looks at its
sub_routers, in registration order, regardless of which sub_router was
included first (see aiogram/dispatcher/router.py's _propagate_event). The
original file relied on exactly this - all four slash commands were
registered together at the very top of the one router, BEFORE any
state-matching catch-all handler further down (e.g. customer_link.py's
link_username, which matches literally any text while in
CustomerLinkStates.waiting_username) - so typing "/buy" mid-flow always
jumps to the buy flow instead of being swallowed by whichever catch-all
happens to be active. Splitting the catch-alls into four separate
sub-routers would silently break that guarantee for three of the four
commands: whichever sub-router got include_router()'d first would have its
own catch-all checked (and could intercept the slash text) before aiogram
ever reached a later sub-router's command handler. Keeping all four commands
as this router's OWN handlers sidesteps the ordering question entirely -
they are always checked first, no matter how the four sub-routers below are
ordered."""
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..admin_scope import resolve_admin_scope
from ..panel_bridge import api, ApiError
from ..keyboards import (
    cancel_kb,
    group_connections_by_purchase,
    home_kb,
    main_menu_kb,
    packages_kb,
    purchases_kb,
    session_count_kb,
    topup_amounts_kb,
)
from ..states import CustomerLinkStates, CustomerPurchaseStates, CustomerTopupStates
from ..utils import packages_message

# Re-exported below (via `noqa: F401` imports) for tutorials.py,
# persistent_menu.py, admin_pending.py, services/auto_approve.py, and
# tests/test_trial_and_receivables.py / tests/test_payment_card_approval_id.py
# / tests/test_receipt_notify_owner.py, all of which reach into `customer`
# (this module) for one or more of these names rather than the file each
# actually now lives in.
from .customer_common import (  # noqa: F401
    NO_PAYMENT_METHOD,
    _blocked_from_buying,
    _clear_state_keep_account,
    _menu_item_enabled,
    _notify_targets,
    _purchase_block_reason,
    _reply_menu_item_disabled,
    _resolve_account,
    _resolve_account_silent,
    _send_menu_footer,
    send_package_extras,
)
from .customer_account import (  # noqa: F401
    _account_text,
    cb_account,
    cb_myid,
    cb_referral,
    cb_sublink,
    cb_support,
    cb_switch_account,
    cb_usage,
    cb_view_connection,
    cb_view_purchase,
    router as _account_router,
)
from .customer_link import (  # noqa: F401
    cb_link_start,
    link_username,
    router as _link_router,
)
from .customer_purchase import (  # noqa: F401
    _distinct_session_counts,
    _give_free_package,
    _loyalty_reward_text,
    _show_payment_screen,
    cb_buy,
    cb_renew,
    enter_comment,
    enter_discount_code,
    enter_referral_code,
    pay_with_balance,
    pick_node,
    pick_package,
    pick_protocol,
    pick_renew_service,
    pick_session_count,
    receive_receipt,
    receive_receipt_wrong_type,
    skip_comment,
    skip_discount_code,
    skip_referral_code,
    router as _purchase_router,
)
from .customer_topup import (  # noqa: F401
    cb_topup_start,
    pick_topup_amount,
    receive_topup_receipt,
    receive_topup_receipt_wrong_type,
    topup_custom_amount,
    router as _topup_router,
)

router = Router(name="customer")
router.include_router(_account_router)
router.include_router(_link_router)
router.include_router(_purchase_router)
router.include_router(_topup_router)


# --------------------------------------------------------- slash commands
# Registered here, BEFORE any state-matching catch-all handler in any of the
# four sub-routers above (e.g. customer_link.py's `link_username`, which
# matches literally any text while in CustomerLinkStates.waiting_username) -
# aiogram tries a router's OWN handlers before its sub_routers, so these are
# always reachable no matter which flow a customer is mid-way through. See
# this file's module docstring for why that requires keeping them here
# rather than in their matching section file. Each one clears any
# in-progress state first so a command always acts as a clean jump, matching
# what this project's own README/help text implies about commands - see also
# runner.py's set_my_commands for where these show up in Telegram's native
# "Menu" button.
@router.message(Command("account"))
async def cmd_account(message: Message, state: FSMContext, bot: Bot) -> None:
    """Slash-command shortcut for "👤 اکانت من"."""
    if not await _menu_item_enabled("cust_account"):
        await _reply_menu_item_disabled(message)
        return
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
    if not await _menu_item_enabled("cust_link"):
        await _reply_menu_item_disabled(message)
        return
    await state.clear()
    await state.set_state(CustomerLinkStates.waiting_username)
    await message.answer("نام کاربری حساب قبلی‌تان را بفرستید:", reply_markup=cancel_kb())


@router.message(Command("buy"))
async def cmd_buy(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "🛒 خرید اکانت جدید"."""
    if not await _menu_item_enabled("cust_buy"):
        await _reply_menu_item_disabled(message)
        return
    if await _blocked_from_buying(message, message.from_user.id):
        return
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
    await message.answer(packages_message(packages), reply_markup=packages_kb(packages, "new"))


@router.message(Command("topup"))
async def cmd_topup(message: Message, state: FSMContext) -> None:
    """Slash-command shortcut for "💰 افزایش اعتبار"."""
    if not await _menu_item_enabled("cust_topup"):
        await _reply_menu_item_disabled(message)
        return
    if await _blocked_from_buying(message, message.from_user.id):
        return
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
