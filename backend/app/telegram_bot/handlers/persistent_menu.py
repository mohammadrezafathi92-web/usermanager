"""The always-visible menu bar at the bottom of a customer's chat.

Requested 2026-09-13, from screenshots of a competitor's bot: "اون دکمه‌ها
پایین ثابت هست این خیلی جذابه".

Until now every customer action lived on an INLINE keyboard - buttons
attached to one particular message. That is fine while you are looking at
that message and useless a minute later: scroll up, or come back tomorrow,
and the shop is somewhere in the history. A ReplyKeyboardMarkup is
different - Telegram pins it under the text box, so «خرید اشتراک» is one
tap away from anywhere in the conversation, forever. For a shop, that is
the whole difference between a bot you remember to open and one you don't.

Nothing about the existing handlers changes. A tap on the bar arrives as an
ordinary text message, and _MenuTap below dresses it up as the inline
button press those handlers already expect - so both routes run exactly
the same code, and a change to «خرید اشتراک» can never apply to one and
not the other.

An admin or seller gets the same bar plus their own quick-actions prepended
ahead of the shop items - see keyboards.persistent_menu_kb's docstring for
why this is one combined bar rather than a separate admin-only one. Below,
_ACTIONS and _LABEL_TO_ACTION cover both label sets, and on_menu_tap tells
the two apart by the "admin_"/"cust_" action prefix.

Two ordering rules this router depends on, both load-bearing:

  * it is registered BEFORE customer.router, so a tap is not swallowed by
    whatever FSM state the customer is stuck in (half-way through typing a
    username, say). Tapping a menu button means "take me there", and the
    state is cleared on the way - the same escape-hatch reasoning as the
    /users command in admin_users.py.
  * it only ever handles a message whose text is EXACTLY one of the
    labels, so ordinary typing (a username, an amount, a receipt caption)
    falls through untouched.
"""
from __future__ import annotations

import logging

from aiogram import Bot, Router, F
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from ..admin_scope import resolve_admin_scope
from ..keyboards import (
    ADMIN_MENU_ITEMS_FULL,
    ADMIN_MENU_ITEMS_SELLER,
    CUSTOMER_MENU_ITEMS,
    persistent_menu_kb,
)
from ..panel_bridge import get_customer_menu_disabled_items_cached
from . import admin_broadcast, admin_pending, admin_users, customer, tutorials

logger = logging.getLogger("telegram_bot")

router = Router(name="persistent_menu")


class _ReplyTarget:
    """Stands in for `call.message`. The handlers edit the message the
    button was attached to; there is no such message here, so an edit
    becomes a new reply. Everything else falls through to the real
    Message."""

    def __init__(self, message: Message):
        self._message = message

    async def edit_text(self, text, **kwargs):
        return await self._message.answer(text, **kwargs)

    async def edit_reply_markup(self, **kwargs):  # nothing to edit
        return None

    def __getattr__(self, name):
        return getattr(self._message, name)


class _MenuTap:
    """Stands in for the CallbackQuery itself."""

    def __init__(self, message: Message):
        self.message = _ReplyTarget(message)
        self.from_user = message.from_user
        self.data = ""
        self._chat_message = message

    async def answer(self, text: str | None = None, show_alert: bool = False, **kwargs):
        """Anything with words in it becomes a real message; a bare
        acknowledgement becomes nothing.

        This has to cover two different callers, which is why it does not
        simply check show_alert. `call.answer("...")` is the little toast on
        an inline button - there is no button here, so the words have to
        land in the chat or the customer sees nothing. But some helpers
        (customer.py's _resolve_account, for instance) check
        `isinstance(target, CallbackQuery)` and, finding this is not one,
        call `target.answer(text, reply_markup=...)` in the MESSAGE sense
        instead. Swallowing that second shape is what made the multi-account
        picker vanish in testing - the same silence the whole bar was
        reported for.
        """
        if text:
            await self._chat_message.answer(text, **kwargs)


# label -> the coroutine the inline button already runs, and what it needs.
# Kept as a table rather than eleven near-identical handlers so that adding
# a menu item is one line in keyboards.CUSTOMER_MENU_ITEMS plus one here.
_ACTIONS = {
    "cust_account": (customer.cb_account, ("state", "bot")),
    "cust_usage": (customer.cb_usage, ("state", "bot")),
    "cust_renew": (customer.cb_renew, ("state",)),
    "cust_buy": (customer.cb_buy, ("state",)),
    "cust_topup": (customer.cb_topup_start, ("state",)),
    "cust_tutorials": (tutorials.cb_tutorials, ()),
    "cust_referral": (customer.cb_referral, ("state",)),
    "cust_support": (customer.cb_support, ()),
    "cust_link": (customer.cb_link_start, ("state",)),
    "cust_myid": (customer.cb_myid, ()),
    # Admin/seller entries. "acting_scope" is a third `needs` kwarg (on top
    # of the "state"/"bot" the customer entries already use) because these
    # handlers normally get it from admin_users.router's own filter-based DI
    # (see admin_users.py's router.message.filter(_admin_scope_filter)) -
    # called directly like this bypasses that filter chain entirely, so
    # on_menu_tap resolves and passes it itself.
    "admin_create": (admin_users.cb_admin_create, ("state", "acting_scope")),
    "admin_list": (admin_users.cb_admin_list, ("state", "acting_scope")),
    "admin_search": (admin_users.cb_admin_search_start, ("state",)),
    "admin_pending": (admin_pending.cb_admin_pending, ()),
    "admin_history": (admin_pending.cb_admin_history, ()),
    "admin_stats": (admin_users.cb_admin_stats, ("acting_scope",)),
    "admin_broadcast": (admin_broadcast.cb_broadcast_start, ("state",)),
    "admin_dm": (admin_broadcast.cb_dm_start, ("state",)),
}

# Full-admin-only actions - the ones ADMIN_MENU_ITEMS_SELLER leaves out.
# Kept as a set rather than reading is_full_admin off keyboards.py's lists
# directly so a seller who somehow still has the label (a stale bar from
# before a demotion, say) gets a clear refusal instead of AdminScope's own
# checks failing in a more confusing way three calls deep.
_ADMIN_FULL_ONLY_ACTIONS = {
    action
    for action, _ in ADMIN_MENU_ITEMS_FULL
    if action not in {a for a, _ in ADMIN_MENU_ITEMS_SELLER}
}

_LABEL_TO_ACTION = {
    label: action
    for action, label in [*CUSTOMER_MENU_ITEMS, *ADMIN_MENU_ITEMS_FULL, *ADMIN_MENU_ITEMS_SELLER]
}


@router.message(F.text.in_(set(_LABEL_TO_ACTION)))
async def on_menu_tap(message: Message, state: FSMContext, bot: Bot) -> None:
    """Reported the same day the bar shipped: "این دکمه ها کار نمیکنه".

    The first version returned here when the tapper turned out to be an
    admin, on the reasoning that the bar is the customer shopfront and an
    admin has their own menu. That was wrong twice over. It made the buttons
    do NOTHING - no reply, no error, the tap just vanished, which is the
    worst possible answer. And it was wrong about who taps: the bar is
    pinned to a CHAT, so anyone who had it before their Telegram id was
    linked to a panel account still has it afterwards - including the panel
    owner testing their own shop, which is exactly who hit this.

    These are customer labels, so the customer screen is the only thing they
    can honestly mean. An admin with no customer account of their own simply
    gets "you have no account yet" from the same handler a customer would -
    an answer, not silence.

    Admin labels are new (2026-09-23, optimization #2) and get the same
    "answer, not silence" treatment for the mirror-image case: the bar is
    pinned to a CHAT, so someone demoted or unlinked after the bar was sent
    still has the buttons in front of them. resolve_admin_scope is asked
    fresh on every tap (never trusted from an earlier one), exactly like
    the inline admin menu does.
    """
    action = _LABEL_TO_ACTION.get(message.text or "")
    entry = _ACTIONS.get(action or "")
    if entry is None:
        # Only reachable if CUSTOMER_MENU_ITEMS/ADMIN_MENU_ITEMS_* grows an
        # item and _ACTIONS does not. Logged rather than ignored - a dead
        # button is invisible to whoever added it and maddening to whoever
        # taps it.
        logger.warning("منوی پایین: برای «%s» هیچ هندلری ثبت نشده است", message.text)
        return

    acting_scope: dict | None = None
    if (action or "").startswith("admin_"):
        acting_scope = await resolve_admin_scope(message.from_user.id)
        if not acting_scope or (action in _ADMIN_FULL_ONLY_ACTIONS and not acting_scope["is_full_admin"]):
            await message.answer("این بخش مخصوص مدیران است.")
            return
    else:
        # Respects the same «منوی مشتری» checkboxes the inline menu does,
        # so an item the panel owner switched off cannot be reached by
        # typing its label either.
        if action in set(await get_customer_menu_disabled_items_cached()):
            # Switched off in «منوی مشتری» while this chat still shows
            # the bar. Said out loud, because a button that answers
            # nothing is indistinguishable from a broken bot.
            await message.answer("این بخش در حال حاضر غیرفعال است.")
            return

    await state.clear()
    handler, needs = entry
    kwargs = {}
    if "state" in needs:
        kwargs["state"] = state
    if "bot" in needs:
        kwargs["bot"] = bot
    if "acting_scope" in needs:
        kwargs["acting_scope"] = acting_scope
    await handler(_MenuTap(message), **kwargs)


async def send_menu_bar(message: Message, scope: dict | None = None) -> None:
    """Puts the bar in place. Telegram only shows a ReplyKeyboardMarkup
    once a message carries one, and it then persists for this chat until
    something replaces it - so this is sent once on /start rather than
    stapled to every reply.

    `scope` is start.py's already-resolved resolve_admin_scope() result -
    passed straight through to persistent_menu_kb() so an admin/seller's
    bar gets their quick-actions prepended without a second lookup here.
    """
    kb = await persistent_menu_kb(scope)
    if kb is None:
        return
    await message.answer("👇 از منوی پایین هر وقت خواستید استفاده کنید.", reply_markup=kb)
