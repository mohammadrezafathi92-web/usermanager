"""Who is this, and whose shop are they standing in?

The authentication boundary for the Telegram Mini App. Everything the mini
app is later allowed to see or buy rests on this file being right, so it is
deliberately small and does one thing.

Telegram hands a Mini App a blob called initData: an ordinary query string
carrying the user, the chat, an issue time, and a `hash`. That hash is an
HMAC over the other fields, keyed by a value derived from THE BOT'S OWN
TOKEN. So a correct signature proves two things at once - the data really
came from Telegram, and it came through that particular bot.

That second half is what makes one URL enough for every reseller on this
install. They share a panel and a hostname; what differs is the bot the
customer opened (AdminUser.own_bot_token). We hold every one of those
tokens, so we try each in turn: whichever token validates the signature
names the reseller whose packages, prices and payment details this customer
should see. The scope comes from a signature, never from the URL - a
reseller id in the path would be a number anyone could edit.

Three rules this file keeps, each of which has been a real vulnerability in
someone else's implementation of the same thing:

  * the comparison is constant-time, so the hash cannot be discovered one
    byte at a time by timing the reply;
  * initData older than MAX_AGE is refused, so a blob captured once (from a
    shared screen, a proxy, a log) does not work forever;
  * `hash` is removed before the check and the remaining fields are sorted,
    exactly as Telegram specifies - a lenient parser here is the usual way
    these get broken.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import logging
from typing import Optional
from urllib.parse import parse_qsl

from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger("telegram_webapp")

# Telegram's own recommendation. Long enough that a slow phone on a bad
# Iranian connection still opens the app, short enough that a leaked blob is
# worthless by the time anyone finds it.
MAX_AGE = dt.timedelta(hours=24)


class WebAppAuthError(Exception):
    """Never carries a reason the caller should show a visitor - the API
    turns this into one flat 401. Telling an unauthenticated caller WHICH
    part of their forgery failed is how you help them fix it."""


def _secret_key(bot_token: str) -> bytes:
    # Telegram's scheme, and the step most often got backwards: the key is
    # HMAC("WebAppData", bot_token), i.e. the literal string is the KEY and
    # the token is the MESSAGE. Swapping them produces a hash that is stable
    # and wrong, so it fails closed rather than dangerously - but it fails
    # for everyone, which is a confusing afternoon.
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def _signature_matches(init_data: str, bot_token: str) -> bool:
    pairs = parse_qsl(init_data, keep_blank_values=True)
    received = next((value for key, value in pairs if key == "hash"), None)
    if not received:
        return False
    check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs) if key != "hash"
    )
    expected = hmac.new(_secret_key(bot_token), check_string.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)


def _candidate_tokens(db: Session) -> list[tuple[Optional[int], str]]:
    """(owner_admin_id, token) for every bot this panel runs.

    The shared bot first, as the common case on a single-reseller install;
    owner_admin_id None for it, matching the convention everywhere else that
    NULL means the superadmin's own.
    """
    candidates: list[tuple[Optional[int], str]] = []
    shared = db.get(models.BotSettings, 1)
    if shared is not None and (shared.bot_token or "").strip():
        candidates.append((None, shared.bot_token.strip()))
    rows = (
        db.query(models.AdminUser.id, models.AdminUser.own_bot_token)
        .filter(models.AdminUser.own_bot_token.isnot(None))
        .order_by(models.AdminUser.id)
        .all()
    )
    for admin_id, token in rows:
        token = (token or "").strip()
        if token:
            candidates.append((admin_id, token))
    return candidates


def verify(db: Session, init_data: str) -> dict:
    """Returns {"telegram_id", "owner_admin_id", "user"} or raises.

    owner_admin_id is whose shop this is: the admin whose own bot was used,
    or None for the shared bot. It is the same value every scoped query in
    this project already takes (routers/bot.py's _visibility_filter), so the
    mini app plugs into the existing scoping rather than inventing a second.
    """
    if not init_data:
        raise WebAppAuthError("no initData")

    matched: Optional[int] = None
    found = False
    for owner_admin_id, token in _candidate_tokens(db):
        if _signature_matches(init_data, token):
            matched, found = owner_admin_id, True
            break
    if not found:
        raise WebAppAuthError("signature did not match any of this panel's bots")

    fields = dict(parse_qsl(init_data, keep_blank_values=True))

    # Checked AFTER the signature on purpose: an unsigned blob's auth_date
    # is worth nothing, so reading it first would just be trusting an
    # attacker's clock.
    try:
        issued = dt.datetime.utcfromtimestamp(int(fields.get("auth_date", "0")))
    except (TypeError, ValueError):
        raise WebAppAuthError("unreadable auth_date")
    age = dt.datetime.utcnow() - issued
    if age > MAX_AGE:
        raise WebAppAuthError(f"initData is {age} old")
    # A timestamp in the future is not a clock-skew curiosity here - it is
    # the shape of a replay attempt buying itself more time. A couple of
    # minutes of tolerance covers real drift.
    if age < -dt.timedelta(minutes=5):
        raise WebAppAuthError("auth_date is in the future")

    try:
        user = json.loads(fields.get("user") or "{}")
    except ValueError:
        raise WebAppAuthError("unreadable user field")
    telegram_id = user.get("id")
    if not isinstance(telegram_id, int):
        raise WebAppAuthError("no telegram id in initData")

    return {
        "telegram_id": telegram_id,
        "owner_admin_id": matched,
        "user": {
            "first_name": user.get("first_name") or "",
            "last_name": user.get("last_name") or "",
            "username": user.get("username") or "",
        },
    }
