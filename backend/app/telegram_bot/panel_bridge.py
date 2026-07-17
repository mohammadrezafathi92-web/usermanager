"""Bridges the bot's handler code (written against a clean async
"api.xxx()" interface returning plain dicts) to the panel's own
app/routers/bot.py functions, called directly in-process instead of over
HTTP. This is what lets the bot run inside the same container as the panel
with zero extra configuration - no PANEL_API_URL, no PANEL_API_KEY, no
separate API key to create; it always talks to whatever is running right
here.

Each router function is a plain Python function (FastAPI's `Depends(get_db)`
is just a default value, easily overridden by passing a real session), so
we can call it directly with a fresh SessionLocal() per call. Router
functions normally get their return value auto-converted to their
`response_model` by FastAPI's routing layer - since we bypass that layer,
functions that return bare ORM objects/dicts (list_nodes, list_packages,
get_payment_info, list_users) are converted explicitly below; functions
that already build and return a schema object themselves (create_user,
get_user, renew, ...) need no extra handling."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

from fastapi import HTTPException

from ..database import SessionLocal
from .. import models, schemas
from ..routers import bot as bot_router
from .config import config


class ApiError(Exception):
    pass


def _scope(owner_admin_id: Optional[int]) -> Optional[int]:
    """Fills in the CALLING bot's own scope (config.bot_owner_admin_id -
    see config.py's threading.local docstring) whenever a handler doesn't
    already pass an explicit owner_admin_id of its own. This is the ONE
    place a per-admin dedicated bot (see AdminUser.own_bot_token) actually
    becomes scoped: every handler file keeps calling api.create_user(...),
    api.list_users(...), etc. exactly as before (most never pass
    owner_admin_id at all - see handlers/admin_pending.py's create_user
    call, "the ONE choke point new customers are created through"), and
    this quietly makes those calls land under the right Admin's tree
    instead of touching the whole panel. For the shared/global bot,
    config.bot_owner_admin_id is None, so this is a complete no-op and
    every call behaves exactly as it always has."""
    return owner_admin_id if owner_admin_id is not None else config.bot_owner_admin_id


async def _call(fn, *args, **kwargs):
    def _run():
        db = SessionLocal()
        try:
            return fn(*args, db=db, **kwargs)
        finally:
            db.close()

    try:
        return await asyncio.to_thread(_run)
    except HTTPException as exc:
        raise ApiError(exc.detail) from exc
    except ApiError:
        raise
    except Exception as exc:  # unexpected - still surface it to the chat instead of crashing the bot
        raise ApiError(str(exc)) from exc


def _dump(obj):
    if obj is None:
        return None
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    return obj


class PanelBridge:
    # ---------------------------------------------------------------- nodes
    async def list_nodes(self) -> list[dict]:
        nodes = await _call(bot_router.list_nodes)
        return _dump([schemas.BotNodeInfo.model_validate(n) for n in nodes])

    # ------------------------------------------------------------ packages
    async def list_packages(self) -> list[dict]:
        packages = await _call(bot_router.list_packages)
        return _dump([schemas.PackageOut.model_validate(p) for p in packages])

    async def get_package_files(self, package_id: int) -> list[dict]:
        """Filename + raw bytes for every file attached to a package, read
        straight off disk - deliberately bypasses PackageFileOut (which has
        no path field on purpose, see schemas.py) since this is only ever
        used in-process by the bot itself, handed straight to aiogram's
        BufferedInputFile, and must never leak into any HTTP response.
        Returns bytes (not a path) so this has the exact same shape as
        remote_bridge.RemoteBridge's version of this method, which has no
        choice but to fetch the bytes over HTTP - that symmetry is what
        lets handlers/customer.py stay identical regardless of whether the
        bot is running in-process here or on a remote server."""

        def _run():
            db = SessionLocal()
            try:
                rows = (
                    db.query(models.PackageFile)
                    .filter(models.PackageFile.package_id == package_id)
                    .all()
                )
                out = []
                for r in rows:
                    try:
                        with open(r.stored_path, "rb") as f:
                            content = f.read()
                    except OSError:
                        continue
                    out.append({"filename": r.filename, "content": content})
                return out
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def get_payment_info(self) -> dict:
        row = await _call(bot_router.get_payment_info)
        return _dump(schemas.PanelSettingsOut.model_validate(row))

    async def get_customer_menu_disabled_items(self) -> list[str]:
        """Which customer main-menu buttons an admin has hidden from
        Settings > ربات (see telegram_bot/keyboards.py's CUSTOMER_MENU_ITEMS
        and models.BotSettings.customer_menu_disabled_items)."""
        row = await _call(bot_router.get_customer_menu_config)
        return row.get("disabled_items", [])

    # -------------------------------------------------------- tutorials
    async def list_tutorials(self) -> list[dict]:
        tutorials = await _call(bot_router.list_tutorials)
        return _dump([schemas.TutorialOut.model_validate(t) for t in tutorials])

    async def get_tutorial_media(self, tutorial_id: int) -> list[dict]:
        """Filename + raw bytes for every photo/video attached to a
        tutorial - same rationale as get_package_files above."""

        def _run():
            db = SessionLocal()
            try:
                rows = (
                    db.query(models.TutorialMedia)
                    .filter(models.TutorialMedia.tutorial_id == tutorial_id)
                    .all()
                )
                out = []
                for r in rows:
                    try:
                        with open(r.stored_path, "rb") as f:
                            content = f.read()
                    except OSError:
                        continue
                    out.append({"kind": r.kind, "filename": r.filename, "content": content})
                return out
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    async def get_tutorial_software_file(self, software_id: int, tutorial_id: Optional[int] = None) -> dict | None:
        """Filename + raw bytes for one uploaded tutorial-software file -
        same rationale as get_tutorial_media above. list_tutorials() above
        already carries each software entry's metadata (name/url/filename)
        via TutorialOut.software, so this is only needed to fetch the bytes
        of an entry that has an uploaded file (stored_path set) rather than
        just a plain download url. tutorial_id is accepted (but unused here)
        only to keep this method's signature identical to
        remote_bridge.RemoteBridge's version, which needs it to build the
        download URL - see that module for why."""

        def _run():
            db = SessionLocal()
            try:
                r = db.get(models.TutorialSoftware, software_id)
                if not r or not r.stored_path:
                    return None
                try:
                    with open(r.stored_path, "rb") as f:
                        content = f.read()
                except OSError:
                    return None
                return {"filename": r.filename, "content": content}
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    # ---------------------------------------------------------- broadcast
    async def list_telegram_user_ids(self) -> list[int]:
        """Every telegram id currently linked to a panel account - used by
        the admin bot's "📢 پیام همگانی" broadcast and the daily
        quota/expiry reminder job."""

        def _run():
            db = SessionLocal()
            try:
                rows = (
                    db.query(models.User.telegram_id)
                    .filter(models.User.telegram_id.isnot(None))
                    .all()
                )
                return [r[0] for r in rows]
            finally:
                db.close()

        return await asyncio.to_thread(_run)

    # ---------------------------------------------------------------- users
    async def create_user(
        self,
        username: str,
        full_name: Optional[str] = None,
        quota_gb: float = 0,
        expire_days: Optional[int] = None,
        telegram_id: Optional[int] = None,
        connections: Optional[list[dict]] = None,
        owner_admin_id: Optional[int] = None,
        package_name: Optional[str] = None,
        package_id: Optional[int] = None,
    ) -> dict:
        payload = schemas.BotCreateUserRequest(
            username=username,
            full_name=full_name,
            quota_gb=quota_gb,
            expire_days=expire_days,
            telegram_id=telegram_id,
            connections=[schemas.BotCreateConnectionSpec(**c) for c in (connections or [])],
            owner_admin_id=_scope(owner_admin_id),
            package_name=package_name,
            package_id=package_id,
        )
        return _dump(await _call(bot_router.create_user, payload))

    async def get_user(self, username: str, owner_admin_id: Optional[int] = None) -> dict:
        return _dump(await _call(bot_router.get_user, username, owner_admin_id=_scope(owner_admin_id)))

    async def get_user_by_telegram(self, telegram_id: int) -> Optional[dict]:
        try:
            return _dump(await _call(bot_router.get_user_by_telegram, telegram_id, owner_admin_id=_scope(None)))
        except ApiError:
            return None

    async def list_users_by_telegram(self, telegram_id: int) -> list[dict]:
        """Every account linked to this telegram id - see the account-picker
        logic in telegram_bot/handlers/customer.py's _resolve_account."""
        return _dump(await _call(bot_router.list_users_by_telegram, telegram_id, owner_admin_id=_scope(None))) or []

    async def get_admin_by_telegram(self, telegram_id: int) -> Optional[dict]:
        """Used by the built-in bot to recognize a linked group-admin (see
        telegram_bot/admin_scope.py) - None if this Telegram id isn't
        linked to any AdminUser."""
        try:
            return _dump(await _call(bot_router.get_admin_by_telegram, telegram_id))
        except ApiError:
            return None

    async def list_users(
        self, page: int = 1, page_size: int = 8, search: Optional[str] = None, owner_admin_id: Optional[int] = None
    ) -> dict:
        result = await _call(
            bot_router.list_users, page=page, page_size=page_size, search=search, owner_admin_id=_scope(owner_admin_id)
        )
        return _dump(schemas.BotUserListPage.model_validate(result))

    async def link_telegram(self, username: str, telegram_id: int) -> dict:
        payload = schemas.BotLinkTelegramRequest(telegram_id=telegram_id)
        return _dump(await _call(bot_router.link_telegram, username, payload))

    async def add_connection(
        self, username: str, node_id: int, protocol: str, flow: str = "", owner_admin_id: Optional[int] = None,
        purchase_batch: Optional[str] = None, package_name: Optional[str] = None,
    ) -> dict:
        spec = schemas.BotCreateConnectionSpec(
            node_id=node_id, protocol=protocol, flow=flow,
            purchase_batch=purchase_batch, package_name=package_name,
        )
        return _dump(await _call(bot_router.add_connection, username, spec, owner_admin_id=_scope(owner_admin_id)))

    async def renew(
        self, username: str, add_gb: float = 0, add_days: int = 0, reset_usage: bool = False,
        owner_admin_id: Optional[int] = None, package_id: Optional[int] = None,
    ) -> dict:
        payload = schemas.BotRenewRequest(add_gb=add_gb, add_days=add_days, reset_usage=reset_usage, package_id=package_id)
        return _dump(await _call(bot_router.renew, username, payload, owner_admin_id=_scope(owner_admin_id)))

    async def reset_usage(self, username: str, owner_admin_id: Optional[int] = None) -> dict:
        return _dump(await _call(bot_router.reset_usage, username, owner_admin_id=_scope(owner_admin_id)))

    async def set_enabled(self, username: str, enabled: bool, owner_admin_id: Optional[int] = None) -> dict:
        return _dump(await _call(bot_router.set_user_enabled, username, enabled, owner_admin_id=_scope(owner_admin_id)))

    async def add_balance(self, username: str, amount: int) -> dict:
        payload = schemas.BotAddBalanceRequest(amount=amount)
        return _dump(await _call(bot_router.add_balance, username, payload))

    async def delete_user(self, username: str, owner_admin_id: Optional[int] = None) -> None:
        await _call(bot_router.delete_user, username, owner_admin_id=_scope(owner_admin_id))

    # ------------------------------------------------ referral & discount
    async def apply_referral(self, username: str, referral_code: str) -> dict:
        """Called once, right after create_user, for a brand-new customer
        who entered someone else's invite code - see
        handlers/admin_pending.py (the receipt-approval handler is the one
        choke point new accounts are created through)."""
        payload = schemas.ReferralApplyRequest(username=username, referral_code=referral_code)
        return _dump(await _call(bot_router.apply_referral, payload))

    async def validate_discount(self, code: str, package_price: int = 0, username: Optional[str] = None) -> dict:
        """Check-as-you-type - does not consume the code."""
        payload = schemas.DiscountValidateRequest(code=code, package_price=package_price, username=username)
        return _dump(await _call(bot_router.validate_discount, payload))

    async def redeem_discount(self, code: str, username: str, package_price: int = 0) -> dict:
        """Called once at final purchase confirmation - actually consumes
        the code (bumps used_count, records a redemption row)."""
        payload = schemas.DiscountRedeemRequest(code=code, username=username, package_price=package_price)
        return _dump(await _call(bot_router.redeem_discount, payload))


# Mode switch: every handler imports `api` from this module and calls
# `api.xxx(...)` without caring which implementation is behind it. Set
# PANEL_API_URL (+ PANEL_API_KEY) when this container is running as a
# REMOTELY-deployed bot instance (see services/remote_deploy.py) - it then
# talks to the mother server's `/api/bot/*` HTTP API instead of the local
# database (which, on a remote deployment, is just an empty throwaway
# sqlite file - see main.py's BOT_STANDALONE_MODE handling). Left unset
# (the default), this container behaves exactly as it always has: the bot
# runs in-process and reads/writes the database directly.
_panel_api_url = os.environ.get("PANEL_API_URL", "").strip()
if _panel_api_url:
    from .remote_bridge import RemoteBridge

    api = RemoteBridge(_panel_api_url, os.environ.get("PANEL_API_KEY", "").strip())
else:
    api = PanelBridge()
