"""The Telegram Mini App's own API.

No admin session anywhere near it. Every request carries the initData blob
Telegram gave the page, in the X-Telegram-InitData header, and
services/telegram_webapp.verify turns that into two facts: which Telegram
user this is, and which bot they came through. The second one is what names
the reseller whose shop they are standing in - see that module for why the
scope has to come from a signature rather than from the URL.

Everything below then reuses the functions the sales bot already calls
(routers/bot.py's list_packages, get_payment_info, list_users_by_telegram),
with the same owner_admin_id those functions already take. That is the whole
point: the Mini App is a second face on the existing shop, not a second shop.
If a price, a package's visibility or a payment card is right in the bot, it
is right here, because it is the same code answering.

Under /api/ so nginx's existing `location /api/` proxy rule covers it with
no configuration change - the same reason routers/subscription.py lives
there.
"""
from __future__ import annotations

import datetime as dt
import logging

import requests

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..services import telegram_webapp
from . import bot as bot_router

logger = logging.getLogger("miniapp")

router = APIRouter(prefix="/api/miniapp", tags=["miniapp"])


def current_visitor(
    x_telegram_init_data: str = Header(None, alias="X-Telegram-InitData"),
    db: Session = Depends(get_db),
) -> dict:
    """The one door in. Returns {"telegram_id", "owner_admin_id", "user"}."""
    try:
        return telegram_webapp.verify(db, x_telegram_init_data or "")
    except telegram_webapp.WebAppAuthError as exc:
        # Logged with the reason, answered without it. An unauthenticated
        # caller learning WHICH part of their forgery failed is being helped
        # to fix it.
        logger.warning("miniapp: initData rejected (%s)", exc)
        raise HTTPException(401, "این صفحه باید از داخل ربات تلگرام باز شود.")


def _shop_title(db: Session, owner_admin_id: int | None) -> str:
    if owner_admin_id is None:
        return ""
    admin = db.get(models.AdminUser, owner_admin_id)
    return admin.username if admin else ""


# The Mini App needs Telegram's own small script for the theme colours and
# initData. It is hosted on telegram.org, which is exactly the host that is
# blocked or throttled on the networks this product is sold into - so the
# page was depending, at load time, on the one domain its customers cannot
# reliably reach.
#
# Served from this panel instead. The backend fetches it once, through the
# same route the bots already use to reach Telegram (the WireGuard tunnel or
# the configured API proxy - see telegram_bot/runner.py), and keeps it in
# memory. The customer's browser then gets it from the same origin the page
# itself came from, which it has obviously just reached.
_TELEGRAM_SCRIPT_URL = "https://telegram.org/js/telegram-web-app.js"
_script_cache: dict[str, object] = {"body": None, "fetched_at": None}
_SCRIPT_TTL = dt.timedelta(hours=12)


@router.get("/telegram-web-app.js")
def telegram_web_app_script():
    from ..telegram_bot.runner import _lookup_telegram_api_proxy_url

    now = dt.datetime.utcnow()
    cached, at = _script_cache["body"], _script_cache["fetched_at"]
    if cached and at and now - at < _SCRIPT_TTL:
        return Response(content=cached, media_type="application/javascript")

    proxies = None
    proxy = _lookup_telegram_api_proxy_url()
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    try:
        resp = requests.get(_TELEGRAM_SCRIPT_URL, timeout=20, proxies=proxies)
        resp.raise_for_status()
        body = resp.content
    except requests.RequestException as exc:
        logger.warning("miniapp: could not fetch Telegram's script (%s)", exc)
        if cached:
            # A stale copy is worth far more than none: without it the page
            # loses the theme and, on clients that only expose initData
            # through this script, its credential too.
            return Response(content=cached, media_type="application/javascript")
        # Valid, empty JavaScript. The page is built to survive its absence
        # (see pages/MiniApp.jsx), and a 502 here would only add a console
        # error to a page that is already coping.
        return Response(content=b"/* telegram-web-app.js unavailable */\n",
                        media_type="application/javascript")

    _script_cache["body"], _script_cache["fetched_at"] = body, now
    logger.info("miniapp: Telegram's script cached (%d bytes)", len(body))
    return Response(content=body, media_type="application/javascript")


@router.get("/home")
def home(visitor: dict = Depends(current_visitor), db: Session = Depends(get_db)):
    """Everything the first screen needs, in one request.

    One round trip rather than four, because this page opens on a phone on
    an Iranian mobile network - where the cost is per request, not per byte.
    """
    owner = visitor["owner_admin_id"]

    packages = bot_router.list_packages(owner_admin_id=owner, db=db)
    accounts = bot_router.list_users_by_telegram(visitor["telegram_id"], db=db, owner_admin_id=owner)
    payment = bot_router.get_payment_info(owner_admin_id=owner, db=db)

    # A customer can hold more than one account under the same Telegram id
    # (see models.User.telegram_id) - the bot shows a picker. Here they are
    # simply listed together, which is what a screen can do and a chat
    # cannot.
    services = []
    wallet = 0
    for account in accounts:
        wallet += getattr(account, "balance", 0) or 0
        for conn in getattr(account, "connections", []) or []:
            services.append(conn)

    return {
        "shop": {
            "title": _shop_title(db, owner),
            "packages": packages,
        },
        "me": {
            "telegram_id": visitor["telegram_id"],
            "name": (visitor["user"].get("first_name") or "").strip(),
            "username": visitor["user"].get("username") or "",
            "wallet": wallet,
            "accounts": accounts,
        },
        "services": services,
        "payment": payment,
    }
