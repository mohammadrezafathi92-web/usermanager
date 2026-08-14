"""The «تبلیغات» section: scheduled adverts posted into a Telegram channel.

Each admin (the superadmin, and every level-2 Admin) owns one AdChannel and a
rotation of AdPosts. A scheduler job walks the channels, and any whose
interval has elapsed gets its next enabled post rendered and sent - through
that admin's OWN bot when they have one, so an Admin advertises their own
packages at their own prices to their own audience.

Two deliberate design points:

- The post body is a TEMPLATE rendered at send time, not frozen text. Prices
  and quotas come from the Package row as it is right now, so changing a price
  in the Packages page is reflected in the next advert automatically. The
  alternative - copying the numbers into the post when it is written - fails
  silently and expensively: the channel keeps advertising last month's price
  and customers arrive expecting it.

- Rotation is by least-recently-sent rather than a stored cursor. A cursor
  breaks as soon as posts are added, deleted or reordered (it points at a
  position that no longer means what it did); "whichever enabled post has
  gone longest without being sent" needs no maintenance and self-corrects.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session, joinedload

from .. import models
from ..database import SessionLocal
from .jalali import fmt_jalali

logger = logging.getLogger("ads")

# Documented in the UI so an admin knows what they can type. Kept small and
# obvious on purpose - a template language nobody can remember is worse than
# no template language.
PLACEHOLDERS = {
    "{package}": "نام پکیج",
    "{quota}": "حجم پکیج (مثلا ۵۰ گیگابایت)",
    "{days}": "مدت پکیج به روز",
    "{price}": "قیمت با جداکننده (مثلا ۱۹۹,۰۰۰)",
    "{code}": "کد تخفیف",
    "{code_expires}": "تاریخ انقضای کد تخفیف (شمسی)",
    "{bot}": "آیدی ربات، مثلا @netcip_bot",
}


def _fmt_price(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _fmt_quota(gb) -> str:
    """`50` rather than `50.0`. quota_gb is a float so half-sizes are
    possible, but whole numbers are the norm and a trailing .0 in a public
    advert looks like a bug."""
    try:
        value = float(gb)
    except (TypeError, ValueError):
        return "—"
    return f"{value:g}"


def render(post: models.AdPost, bot_username: str | None = None) -> str:
    """Fills the placeholders. Anything with no data behind it renders as an
    em dash rather than leaving a raw `{price}` visible in a public channel."""
    pkg = post.package
    code = post.discount_code

    values = {
        "{package}": (pkg.name if pkg else "—"),
        "{quota}": (f"{_fmt_quota(pkg.quota_gb)} گیگابایت" if pkg and pkg.quota_gb else ("نامحدود" if pkg else "—")),
        "{days}": (str(pkg.duration_days) if pkg and pkg.duration_days else "—"),
        "{price}": (_fmt_price(pkg.price) if pkg else "—"),
        "{code}": (code.code if code else "—"),
        "{code_expires}": (fmt_jalali(code.expires_at, with_time=False) if code and code.expires_at else "—"),
        "{bot}": (f"@{bot_username}" if bot_username else ""),
    }
    text = post.body or ""
    for key, val in values.items():
        text = text.replace(key, val)
    return text.strip()


def _bot_for(db: Session, channel: models.AdChannel) -> tuple[str | None, str | None]:
    """(token, bot_username) for this channel's owner - their own dedicated
    bot if they have one enabled, otherwise the shared panel bot."""
    admin = channel.owner_admin
    if admin is not None and admin.own_bot_token and admin.own_bot_enabled:
        from ..telegram_bot import runner as telegram_bot_runner

        status = telegram_bot_runner.get_admin_bot_status(admin.id) or {}
        return admin.own_bot_token, status.get("bot_username")

    row = db.query(models.BotSettings).first()
    token = (row.bot_token or "").strip() if row else ""
    from ..telegram_bot import runner as telegram_bot_runner

    status = telegram_bot_runner.get_status() or {}
    return (token or None), status.get("bot_username")


def next_post(channel: models.AdChannel) -> models.AdPost | None:
    """Least-recently-sent enabled post. A post whose package was deleted is
    skipped: it would render as a row of dashes, which is worse than not
    advertising at all."""
    candidates = [
        p for p in channel.posts
        if p.enabled and not (p.package_id and p.package is None)
    ]
    if not candidates:
        return None
    # Never-sent posts first (last_sent_at NULL), then oldest, then by the
    # admin's own ordering as the tie-break.
    return sorted(
        candidates,
        key=lambda p: (p.last_sent_at or dt.datetime.min, p.sort_order or 0, p.id),
    )[0]


def send_post(db: Session, channel: models.AdChannel, post: models.AdPost, *, manual: bool = False) -> tuple[bool, str | None]:
    """Renders and posts one advert. Returns (ok, error).

    `manual` marks a "send now" from the panel - it still records the message
    id so the delete-previous behaviour stays consistent, but it does not
    touch last_sent_at on the CHANNEL, so a manual test does not push the
    automatic schedule out by a whole interval.
    """
    from ..telegram_bot import runner as telegram_bot_runner

    if not channel.chat_id:
        return False, "آیدی کانال تنظیم نشده است"

    token, bot_username = _bot_for(db, channel)
    if not token:
        return False, "توکن ربات تنظیم نشده است"

    text = render(post, bot_username)
    if not text:
        return False, "متن پست خالی است"

    button_url = f"https://t.me/{bot_username}?start=ad{post.id}" if bot_username else None

    ok, message_id, error = telegram_bot_runner.send_post_sync(
        channel.chat_id,
        text,
        photo_path=post.image_path,
        button_text=(post.button_text or None) if button_url else None,
        button_url=button_url,
        token=token,
    )
    if not ok:
        channel.last_error = error
        return False, error

    # Only once the new post is safely up - deleting first would leave the
    # channel with no advert at all if the send then failed.
    if channel.delete_previous and channel.last_message_id:
        telegram_bot_runner.delete_message_sync(channel.chat_id, channel.last_message_id, token=token)

    now = dt.datetime.utcnow()
    channel.last_message_id = message_id
    channel.last_error = None
    channel.sent_count = (channel.sent_count or 0) + 1
    if not manual:
        channel.last_sent_at = now
    post.last_sent_at = now
    post.sent_count = (post.sent_count or 0) + 1
    return True, None


def _local(now: dt.datetime) -> dt.datetime:
    """UTC -> the panel's display timezone. Quiet hours are set by a human
    against their own clock, so the comparison has to happen there."""
    from .jalali import get_display_offset

    return now + dt.timedelta(minutes=get_display_offset())


def in_active_window(channel: models.AdChannel, now: dt.datetime) -> bool:
    """Whether local time is inside the channel's allowed posting hours.

    Handles a window that wraps past midnight (e.g. 21 to 2) as well as the
    ordinary case, and treats from == to as "no restriction" rather than as
    a zero-length window - the latter reading would silently stop a channel
    forever, and an admin who sets both to the same value clearly means
    "any time", not "never"."""
    start = channel.active_from_hour if channel.active_from_hour is not None else 0
    end = channel.active_to_hour if channel.active_to_hour is not None else 0
    if start == end:
        return True
    hour = _local(now).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end      # wraps past midnight


def next_allowed_time(channel: models.AdChannel, at: dt.datetime) -> dt.datetime:
    """The first moment from `at` onwards that falls inside the window - used
    to predict the schedule shown in the panel."""
    if in_active_window(channel, at):
        return at
    # At most 24 hourly steps: the window is defined in whole hours, so a
    # scan is exact here and far clearer than modular arithmetic.
    probe = at.replace(minute=0, second=0, microsecond=0)
    for _ in range(25):
        probe += dt.timedelta(hours=1)
        if in_active_window(channel, probe):
            return probe
    return at


def _due(channel: models.AdChannel, now: dt.datetime) -> bool:
    if not channel.enabled or not channel.chat_id:
        return False
    if not in_active_window(channel, now):
        return False
    if channel.last_sent_at is None:
        return True
    return now - channel.last_sent_at >= dt.timedelta(hours=max(1, channel.interval_hours or 6))


def upcoming(channel: models.AdChannel, count: int = 6) -> list[dict]:
    """Which advert goes out when, for the next `count` slots.

    Simulates the real rotation rather than describing it, so what the panel
    shows cannot drift from what the scheduler actually does - the two would
    otherwise be two implementations of the same rule.
    """
    posts = [
        p for p in channel.posts
        if p.enabled and not (p.package_id and p.package is None)
    ]
    if not posts:
        return []

    interval = dt.timedelta(hours=max(1, channel.interval_hours or 6))
    now = dt.datetime.utcnow()
    at = (channel.last_sent_at + interval) if channel.last_sent_at else now
    if at < now:
        at = now

    # Copy of the ordering keys so the simulation can advance them without
    # touching the database rows.
    state = [[p.last_sent_at or dt.datetime.min, p.sort_order or 0, p.id, p] for p in posts]
    out = []
    for _ in range(count):
        at = next_allowed_time(channel, at)
        state.sort(key=lambda s: (s[0], s[1], s[2]))
        chosen = state[0]
        out.append({"at": at, "post_id": chosen[3].id, "title": chosen[3].title or f"#{chosen[3].id}"})
        chosen[0] = at          # it has now "been sent" at this slot
        at = at + interval
    return out


def run_due_campaigns() -> int:
    """Scheduler entry point. Returns how many adverts were posted.

    Runs often (every 10 minutes) and decides per channel whether its own
    interval has elapsed, rather than each channel owning a scheduler job -
    channels are created and reconfigured from the panel at runtime, and
    re-registering jobs on every settings change is a synchronisation problem
    with nothing to gain.
    """
    db = SessionLocal()
    sent = 0
    try:
        now = dt.datetime.utcnow()
        channels = (
            db.query(models.AdChannel)
            .options(joinedload(models.AdChannel.posts), joinedload(models.AdChannel.owner_admin))
            .filter(models.AdChannel.enabled == True)  # noqa: E712
            .all()
        )
        for channel in channels:
            if not _due(channel, now):
                continue
            post = next_post(channel)
            if post is None:
                continue
            try:
                if channel.auto_send:
                    ok, error = send_post(db, channel, post)
                    if ok:
                        sent += 1
                    else:
                        logger.warning("ad post failed for channel %s: %s", channel.id, error)
                else:
                    _send_for_approval(db, channel, post)
                    # Counts as "handled" for scheduling purposes: without
                    # this the same post would be re-offered every 10 minutes
                    # until the admin got around to approving it.
                    channel.last_sent_at = now
            except Exception:
                logger.exception("ad campaign failed for channel %s", channel.id)
                db.rollback()
                continue
        db.commit()
    except Exception:
        logger.exception("run_due_campaigns failed")
        db.rollback()
    finally:
        db.close()
    return sent


def _send_for_approval(db: Session, channel: models.AdChannel, post: models.AdPost) -> None:
    """Approval mode: the advert goes to the owning admin in Telegram as a
    preview instead of to the channel."""
    from ..telegram_bot import runner as telegram_bot_runner

    admin = channel.owner_admin
    if admin is None or not admin.telegram_id:
        channel.last_error = "برای حالت تأیید دستی، آیدی تلگرام ادمین باید ثبت شده باشد"
        return

    token, bot_username = _bot_for(db, channel)
    preview = (
        "📢 <b>پیش‌نمایش تبلیغ</b>\n"
        f"کانال: {channel.chat_id}\n"
        "برای انتشار، از بخش تبلیغات پنل دکمه «ارسال فوری» را بزنید.\n\n"
        "———\n\n" + render(post, bot_username)
    )
    telegram_bot_runner.send_message_sync(admin.telegram_id, preview, token=token)
