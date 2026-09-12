"""Is this numeric Telegram id a person, or one of the panel's own bots?

Reported 2026-09-12, from the logs of a live panel:

    TelegramForbiddenError: Telegram server says -
    Forbidden: the bot can't send messages to the bot

Telegram answers that when a bot is asked to message a chat that belongs to
another bot. Nothing in the panel ever invents such a chat id, so one was
typed in - and there is really only one way that happens. A bot token looks
like

    8012345678:AAHk9...

and the part before the colon IS that bot's Telegram user id. Anyone who has
just created a bot in @BotFather has that number in front of them, and
"آیدی عددی ادمین" is the next box they fill in. The number is perfectly
valid, the panel accepted it, and from then on every receipt, every approval
prompt, every backup and every notification aimed at that "admin" was thrown
away by Telegram - silently, because all of those sends are deliberately
best-effort and swallow their errors so a failed notification can never undo
a completed purchase.

Worse, it does not only break notifications. telegram_bot/admin_scope.py and
runner.start_admin_bot both key the admin menu off the same stored id, so a
bot id there also means the real person is never recognised as an admin in
their own bot - they just get the customer menu, with nothing anywhere
saying why.

So the id is refused where it is entered, which is the only place anyone is
still looking at a screen. The check is exact and offline: it compares
against the token prefixes the panel itself stores (the shared bot's, and
every admin's own_bot_token), so it can only ever reject a number that
really is one of this panel's bots - no Telegram API call, no guessing by
magnitude, no false positives for an ordinary user whose id happens to look
similar.
"""
from __future__ import annotations

from typing import Iterable, Optional

from sqlalchemy.orm import Session

from .. import models


def id_from_token(token: str | None) -> Optional[int]:
    """The bot's own Telegram user id, which is the token's prefix."""
    prefix = (token or "").strip().split(":", 1)[0]
    return int(prefix) if prefix.isdigit() else None


def panel_bot_ids(db: Session) -> dict[int, str]:
    """{telegram id of one of this panel's bots: a name for it}."""
    found: dict[int, str] = {}
    shared = db.get(models.BotSettings, 1)
    if shared is not None:
        bot_id = id_from_token(shared.bot_token)
        if bot_id:
            found[bot_id] = "ربات مشترک پنل"
    rows = (
        db.query(models.AdminUser.username, models.AdminUser.own_bot_token)
        .filter(models.AdminUser.own_bot_token.isnot(None))
        .all()
    )
    for username, token in rows:
        bot_id = id_from_token(token)
        if bot_id:
            found.setdefault(bot_id, f"ربات اختصاصی «{username}»")
    return found


def describe_if_bot(db: Session, telegram_id: int | None) -> Optional[str]:
    """A human-readable reason, or None when this id is fine to use."""
    if not telegram_id:
        return None
    which = panel_bot_ids(db).get(int(telegram_id))
    if not which:
        return None
    return (
        f"آیدی عددی {telegram_id} متعلق به «{which}» است، نه به یک کاربر تلگرام. "
        "این عدد همان بخش ابتدایی توکن ربات (قبل از «:») است - تلگرام اجازه نمی‌دهد "
        "رباتی به ربات دیگری پیام بدهد، پس هیچ اعلان و رسیدی به این آیدی نمی‌رسد و "
        "منوی ادمین هم فعال نمی‌شود. آیدی عددی خودتان را از @userinfobot بگیرید."
    )


def bot_ids_among(db: Session, telegram_ids: Iterable[int]) -> list[int]:
    """Which of these are the panel's own bots - for warning about a list
    that is already saved, rather than refusing a new value."""
    known = panel_bot_ids(db)
    return sorted({int(i) for i in telegram_ids if int(i) in known})
