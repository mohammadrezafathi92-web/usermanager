"""Daily background job: warns telegram-linked customers who are close to
running out - either quota (>=80% used) or time (expiring within 3 days) -
via the built-in sales bot. Each warning is sent at most once per
occurrence (tracked by notified_quota_80/notified_expiry_soon) so a
customer who stays over the threshold for a week doesn't get pinged every
single day; the flag resets on its own once the underlying condition clears
(quota topped up/reset, or expiry pushed back out), so they get warned
again next time they approach the edge.

IMPORTANT (fixed 2026-08-10): warnings are evaluated PER SERVICE, i.e. per
models.Purchase, not just on the user's combined fields. Once services
became independently-enforced Purchases (see services/purchase_migration.py)
the user-level used_bytes/expire_at stopped moving for any purchase-linked
connection - quota_manager's _apply_delta increments the PURCHASE's
counters instead - so a customer could burn through a service completely
while User.used_bytes sat frozen at its migration-time value and no warning
ever fired. Both levels are checked now: every Purchase on its own
counters, plus the user's own pool for legacy connections that still have
no purchase_id."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from .jalali import fmt_jalali_long
from ..telegram_bot import runner as telegram_bot_runner

logger = logging.getLogger("notify")

QUOTA_WARN_RATIO = 0.8
EXPIRY_WARN_DAYS = 3


def _service_label(user: models.User, holder) -> str:
    """What to call the thing running out, in the customer's message - the
    service's own package name when we have one (a customer with several
    services needs to know WHICH), falling back to their username."""
    name = getattr(holder, "package_name_snapshot", None) or getattr(holder, "comment", None)
    return f"{name} ({user.username})" if name else user.username


def _quota_message(user: models.User, holder, used: int, total: int) -> str:
    used_gb = used / (1024 ** 3)
    total_gb = total / (1024 ** 3)
    pct = int(used / total * 100) if total else 0
    return (
        f"⚠️ حجم سرویس <b>{_service_label(user, holder)}</b> رو به اتمام است.\n\n"
        f"مصرف‌شده: {used_gb:.1f} از {total_gb:.1f} گیگابایت ({pct}٪)\n\n"
        "برای جلوگیری از قطع سرویس، از طریق ربات تمدید/افزایش حجم کنید."
    )


def _expiry_message(user: models.User, holder, expire_at: dt.datetime, days_left: int) -> str:
    when = "امروز/فردا" if days_left <= 1 else f"{days_left} روز دیگر"
    return (
        f"⏰ سرویس <b>{_service_label(user, holder)}</b> {when} منقضی می‌شود "
        f"(تاریخ انقضا: {fmt_jalali_long(expire_at)}).\n\n"
        "برای جلوگیری از قطع سرویس، از طریق ربات تمدید کنید."
    )


def _process_quota(db: Session, user: models.User, holder, total: int, used: int) -> bool:
    """`holder` is whatever carries the notified_* flags for this allotment -
    a models.Purchase for an independent service, or the User itself for
    the legacy shared pool. Returns True if a message was sent."""
    if not total:
        return False  # unlimited quota - never applicable
    over_threshold = (used or 0) / total >= QUOTA_WARN_RATIO

    if not over_threshold:
        if holder.notified_quota_80:
            holder.notified_quota_80 = False  # reset - ready to warn again next time
        return False

    if holder.notified_quota_80:
        return False  # already warned for this occurrence

    if not user.telegram_id:
        return False

    if telegram_bot_runner.send_message_sync(user.telegram_id, _quota_message(user, holder, used or 0, total)):
        holder.notified_quota_80 = True
        return True
    return False


def _process_expiry(db: Session, user: models.User, holder, expire_at: dt.datetime | None) -> bool:
    if not expire_at:
        if holder.notified_expiry_soon:
            holder.notified_expiry_soon = False
        return False

    now = dt.datetime.utcnow()
    days_left = (expire_at - now).days
    soon = expire_at > now and expire_at <= now + dt.timedelta(days=EXPIRY_WARN_DAYS)

    if not soon:
        if holder.notified_expiry_soon:
            holder.notified_expiry_soon = False
        return False

    if holder.notified_expiry_soon:
        return False

    if not user.telegram_id:
        return False

    if telegram_bot_runner.send_message_sync(
        user.telegram_id, _expiry_message(user, holder, expire_at, max(days_left, 0))
    ):
        holder.notified_expiry_soon = True
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
            # 1) every independently-enforced service, on its own counters
            for purchase in user.purchases:
                if purchase.status == models.UserStatus.disabled:
                    continue
                if _process_quota(db, user, purchase, purchase.quota_bytes or 0, purchase.used_bytes or 0):
                    quota_sent += 1
                if _process_expiry(db, user, purchase, purchase.expire_at):
                    expiry_sent += 1

            # 2) the user's own legacy pool - only meaningful while they
            # still have connections not tied to a Purchase (everything
            # else moved to the per-service model above, and warning on a
            # frozen leftover total would just be noise).
            if any(c.purchase_id is None for c in user.connections):
                if _process_quota(db, user, user, user.total_quota_bytes or 0, user.used_bytes or 0):
                    quota_sent += 1
                if _process_expiry(db, user, user, user.expire_at):
                    expiry_sent += 1

        db.commit()
        if quota_sent or expiry_sent:
            logger.info("daily notify job: quota warnings=%d expiry warnings=%d", quota_sent, expiry_sent)
    except Exception:
        logger.exception("run_daily_notify_job failed")
        db.rollback()
    finally:
        db.close()
