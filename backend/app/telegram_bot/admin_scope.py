"""Resolves what kind of "admin" (if any) a given Telegram user is, for the
built-in interactive bot.

Two independent notions of "admin" now exist:
  1. The bot's own global admin_ids list (BotSettings, set from the panel's
     "ربات تلگرام" page) - sees/manages EVERYTHING (every customer, pending
     approvals, broadcast).
  2. A panel AdminUser (sub-admin or the superadmin) who linked their own
     numeric Telegram id from the "مدیریت ادمین‌ها" page - can manage only
     the users in their own group (owner_admin_id) straight from the bot,
     independent of the global admin_ids list above.

Everything in handlers/admin_users.py (and start.py's welcome/help text)
should go through resolve_admin_scope() instead of calling config.is_admin()
directly, so both kinds of admin are recognized consistently."""
from __future__ import annotations

from typing import Optional, TypedDict

from .config import config
from .panel_bridge import api, ApiError


class AdminScope(TypedDict):
    # None for a global/config admin (sees everyone); a real admin id for a
    # linked group-admin, used to scope every users/... API call to just
    # their own group.
    owner_admin_id: Optional[int]
    # True only for the global config-based admin list OR a linked
    # superadmin - grants the full admin menu (pending approvals,
    # broadcast). A linked non-superadmin group-admin gets False here even
    # though they're still "some kind of admin".
    is_full_admin: bool
    username: Optional[str]


async def resolve_admin_scope(tg_id: int) -> Optional[AdminScope]:
    """None means this Telegram user is a regular customer - anything else
    means show them some flavor of the admin menu."""
    if config.is_admin(tg_id):
        return {"owner_admin_id": None, "is_full_admin": True, "username": None}
    try:
        info = await api.get_admin_by_telegram(tg_id)
    except ApiError:
        info = None
    if not info:
        return None
    if info.get("is_superadmin"):
        # A superadmin who linked their own Telegram id gets the exact same
        # full menu as a global config admin - they already see every
        # group in the panel, no reason the bot should be more limited.
        return {"owner_admin_id": None, "is_full_admin": True, "username": info.get("username")}
    return {"owner_admin_id": info["id"], "is_full_admin": False, "username": info.get("username")}
