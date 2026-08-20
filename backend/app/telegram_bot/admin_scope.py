"""Resolves what kind of "admin" (if any) a given Telegram user is, for the
built-in interactive bot.

There is ONE question here - "who is this person, in panel terms" - and the
answer must be the panel's own, because every screen the bot then shows is
drawn from panel data. Two things used to answer it independently:

  1. The bot's global admin_ids list (BotSettings, typed into the panel's
     "ربات تلگرام" page as plain numbers).
  2. A panel AdminUser who linked their numeric Telegram id.

They disagreed. A superadmin resolved through (1) or (2) got
`owner_admin_id: None`, which the API treats as "no filter" - so in the bot
they saw every Admin's customers, while the same account in the panel sees
only its own plus the ownerless ones (hierarchy.owned_admin_ids, isolation
the panel owner asked for explicitly). Same person, same account, two
different customer lists depending on which door they came through.

Now a linked panel account always wins, and its scope is its own real id,
so the bot asks the panel the same question the panel asks itself. The
admin_ids list survives only as a fallback for ids with NO panel account
behind them - see the warning on `is_unscoped` below.
"""
from __future__ import annotations

import logging
from typing import Optional, TypedDict

from .config import config
from .panel_bridge import api, ApiError

logger = logging.getLogger("telegram_bot")

ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_SELLER = "seller"
# Not a panel role: a Telegram id that appears in BotSettings.admin_ids but
# matches no AdminUser at all. It has no account, so it has no scope, no
# password, no audit trail and no role - and it keeps working even after the
# account it was meant to represent is deleted. Kept working for backward
# compatibility, reported by scripts/check_hierarchy.py so it can be
# noticed and replaced with a real linked account.
ROLE_CONFIG_ONLY = "config"


class AdminScope(TypedDict):
    # The real panel account id this person acts as. None ONLY for
    # ROLE_CONFIG_ONLY, where there is no account to name.
    owner_admin_id: Optional[int]
    role: str
    # True = show the full admin menu. Now derived from the role rather
    # than stored separately, so the menu and the data scope can never
    # describe different people.
    is_full_admin: bool
    # True = this session is not scoped to any account and therefore sees
    # every customer in the panel. Only ever true for ROLE_CONFIG_ONLY.
    is_unscoped: bool
    username: Optional[str]
    # Which accounts' rows this admin may act on, straight from the panel's
    # own hierarchy. None (with is_unscoped) means "everything". Used for
    # the pending-receipt store, which lives in the bot's own sqlite file
    # and so cannot ask the panel per query.
    owner_ids: Optional[set]
    include_unowned: bool


async def resolve_admin_scope(tg_id: int) -> Optional[AdminScope]:
    """None means this Telegram user is a regular customer - anything else
    means show them some flavour of the admin menu."""
    try:
        info = await api.get_admin_by_telegram(tg_id)
    except ApiError:
        info = None

    if info:
        if info.get("is_superadmin"):
            role = ROLE_SUPERADMIN
        else:
            role = (info.get("role") or "").strip() or ROLE_SELLER
        return {
            # The account's own id, even for a superadmin. Passing None
            # here is what made the bot unscoped, and a superadmin is not
            # an exception to the panel's isolation rule - they are the
            # account it was designed around.
            "owner_admin_id": info["id"],
            "role": role,
            # A Seller gets the reduced menu; both Admin tiers get the
            # full one, each scoped to their own tree by the API.
            "is_full_admin": role in (ROLE_SUPERADMIN, ROLE_ADMIN),
            "is_unscoped": False,
            "username": info.get("username"),
            # Falls back to just this account rather than to None: an older
            # panel build that does not send owner_ids must make the bot
            # narrower, never wider.
            "owner_ids": set(info.get("owner_ids") or [info["id"]]),
            "include_unowned": bool(info.get("include_unowned")),
        }

    if config.is_admin(tg_id):
        logger.warning(
            "تلگرام‌آیدی %s فقط در فهرست admin_ids ربات است و هیچ حساب پنلی ندارد - "
            "این دسترسی محدود به هیچ ادمینی نیست و همه‌ی مشتریان را می‌بیند",
            tg_id,
        )
        return {
            "owner_admin_id": None,
            "role": ROLE_CONFIG_ONLY,
            "is_full_admin": True,
            "is_unscoped": True,
            "username": None,
            "owner_ids": None,
            "include_unowned": True,
        }

    return None
