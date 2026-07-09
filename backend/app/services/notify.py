"""Daily background job: warns telegram-linked customers who are close to
running out - either quota (>=80% used) or time (expiring within 3 days) -
via the built-in sales bot. Each warning is sent at most once per
occurrence (tracked by User.notified_quota_80/notified_expiry_soon) so a
customer who stays over the threshold for a week doesn't get pinged every
single day; the flag resets on its own once the underlying condition clears
(quota topped up/reset, or expiry pushed back out), so they get warned
again next time they approach the edge."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from ..telegram_bot import runner as telegram_bot_runner

logger = logging.getLogger("notify")

QUOTA_WARN_RATIO = 0.8
EXPIRY_WARN_DAYS = 3


def _quota_message(user: models.User) -> str:
    used_gb = (user.used_bytes or 0) / (1024 ** 3)
    total_gb = (user.total_quota_bytes or 0) / (1024 ** 3)
    pct = int((user.used_bytes or 0) / user.total_quota_bytes * 100) if user.total_quota_bytes else 0
    return (
        f"⚠️ حجم مصرفی حساب <b>{user.username}</b> رو به اتمام است.\n\n"
        f"مصرف‌شده: {used_gb:.1f} از {total_gb:.1f} گیگابایت ({pct}٪)\n\n"
        "برای جلوگیری از قطع سرویس، از طریق ربات تمدید/افزایش حجم کنید."
    )


def _expiry_message(user: models.User, days_left: int) -> str:
    when = "امروز/فردا" if days_left <= 1 else f"{days_left} روز دیگر"
    return (
        f"⏰ حساب <b>{user.username}</b> {when} منقضی می‌شود "
        f"(تاریخ انقضا: {user.expire_at.strftime('%Y-%m-%d')}).\n\n"
        "برای جلوگیری از قطع سرویس، از طریق ربات تمدید کنید."
    )


def _process_quota(db: Session, user: models.User) -> bool:
    """Returns True if a message was sent."""
    if not user.total_quota_bytes:
        return False  # unlimited quota - never applicable
    ratio = (user.used_bytes or 0) / user.total_quota_bytes
    over_threshold = ratio >= QUOTA_WARN_RATIO

    if not over_threshold:
        if user.notified_quota_80:
            user.notified_quota_80 = False  # reset - ready to warn again next time
        return False

    if user.notified_quota_80:
        return False  # already warned for this occurrence

    if not user.telegram_id:
        return False

    if telegram_bot_runner.send_message_sync(user.telegram_id, _quota_message(user)):
        user.notified_quota_80 = True
        return True
    return False


def _process_expiry(db: Session, user: models.User) -> bool:
    if not user.expire_at:
        if user.notified_expiry_soon:
            user.notified_expiry_soon = False
        return False

    now = dt.datetime.utcnow()
    days_left = (user.expire_at - now).days
    soon = user.expire_at > now and user.expire_at <= now + dt.timedelta(days=EXPIRY_WARN_DAYS)

    if not soon:
        if user.notified_expiry_soon:
            user.notified_expiry_soon = False
        return False

    if user.notified_expiry_soon:
        return False

    if not user.telegram_id:
        return False

    if telegram_bot_runner.send_message_sync(user.telegram_id, _expiry_message(user, max(days_left, 0))):
        user.notified_expiry_soon = True
        return True
    return False


def run_daily_notify_job() -> None:
    db = SessionLocal()
    try:
        users = (
            db.query(models.User)
            .filter(models.User.telegram_id.isnot(None))
            .filter(models.User.status != models.UserStatus.disabled)
            .all()
        )
        quota_sent = 0
        expiry_sent = 0
        for user in users:
            if _process_quota(db, user):
                quota_sent += 1
            if _process_expiry(db, user):
                expiry_sent += 1
        db.commit()
        if quota_sent or expiry_sent:
            logger.info("daily notify job: quota warnings=%d expiry warnings=%d", quota_sent, expiry_sent)
    except Exception:
        logger.exception("run_daily_notify_job failed")
        db.rollback()
    finally:
        db.close()
