"""The free sample, and the three rules that keep it from being farmed.

A trial is an ordinary models.Package with is_trial set (see that column's
docstring for why it is not its own entity). What makes it different is
entirely here, in one module, because the rules only work as a set:

  1. Once per Telegram id, across every account that id holds.
  2. Only for someone who has never bought anything.
  3. A daily ceiling for the whole shop.

Each on its own is trivially defeated. (1) alone: the customer buys once,
then takes a trial every month for ever. (2) alone: they open a second
Telegram account. (3) alone: it caps the damage without preventing any of
it. Together they say "one free sample, to someone who has not bought yet,
and no more than N a day whatever happens" - which is what a free sample
is.

Deliberately NOT applied to routers/users.py. An admin granting a package
by hand from the panel is not a customer helping themselves, and an admin
who wants to give a second trial to someone they are talking to should be
able to. Everything below guards the SELF-SERVICE surfaces only: the bot's
own purchase flow and the Mini App's checkout.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models


# What a sample may be, at most, when a reseller defines one.
#
# The reason there is a ceiling at all: is_trial waives the charge to the
# reseller (routers/bot.py's _charge_seller). Without a limit, a reseller
# could tick that box on a 500 GB annual package - or simply edit the
# 200 MB one upward - and hand out a year of service that costs them
# nothing and the platform everything. Reported 2026-09-15: «بعد از ساخت
# تست رایگان ... میتونه تایم و حجمش رو هرچقدر که بخواد بالا ببره».
#
# A superadmin is exempt. It is their platform and their cost, so a
# ceiling on them would be a rule with nobody to enforce it for.
TRIAL_MAX_GB = 1.0
TRIAL_MAX_DAYS = 7


def is_trial(package: Optional[models.Package]) -> bool:
    return bool(package is not None and getattr(package, "is_trial", False))


def ensure_within_limits(admin, *, quota_gb, duration_days, price) -> None:
    """Refuses a trial that is too generous to be a trial.

    Checked on the values the package will HAVE, by the caller, on both
    create and update - an edit is exactly how the hole was found, and a
    check that only runs at creation is a check that runs once and then
    never again on the thing it was protecting.
    """
    if getattr(admin, "is_superadmin", False):
        return

    # ZERO IS NOT SMALL. In this schema 0 means UNLIMITED for quota_gb and
    # 0/None means NEVER EXPIRES for duration_days (see models.Package), so
    # a ceiling written only as `> MAX` has a hole at the bottom: clearing
    # the field sails under it and produces the most generous package the
    # panel can express. Reported 2026-09-15 with a screenshot reading
    # «نامحدود / بدون انقضا»: «با خالی کردن میشه تست رو نامحدود کرد».
    #
    # Third time today that a falsy zero has meant the opposite of small -
    # the others were a free package's price skipping the payment branch,
    # and the same price passing a "has a value" test. Worth stating: in
    # this codebase 0 is a value with meaning, never an absence.
    quota = 0.0 if quota_gb is None else float(quota_gb)
    if quota <= 0:
        raise HTTPException(
            400,
            "حجم سرویس تست را خالی یا صفر نگذارید - صفر یعنی نامحدود. "
            f"عددی بین ۰ تا {TRIAL_MAX_GB:g} گیگابایت وارد کنید.",
        )
    if quota > TRIAL_MAX_GB:
        raise HTTPException(
            400,
            f"حجم سرویس تست حداکثر می‌تواند {TRIAL_MAX_GB:g} گیگابایت باشد. "
            "برای حجم بیشتر یک پکیج عادی بسازید.",
        )

    days = 0 if duration_days is None else int(duration_days)
    if days <= 0:
        raise HTTPException(
            400,
            "مدت سرویس تست را خالی یا صفر نگذارید - صفر یعنی بدون انقضا. "
            f"عددی بین ۱ تا {TRIAL_MAX_DAYS} روز وارد کنید.",
        )
    if days > TRIAL_MAX_DAYS:
        raise HTTPException(
            400,
            f"مدت سرویس تست حداکثر می‌تواند {TRIAL_MAX_DAYS} روز باشد. "
            "برای مدت بیشتر یک پکیج عادی بسازید.",
        )
    # A priced "trial" is a contradiction, and a dangerous one: it would be
    # sold like a package while costing the reseller nothing.
    if price is not None and int(price) != 0:
        raise HTTPException(400, "قیمت سرویس تست باید صفر باشد.")


def _account_ids_for(db: Session, telegram_id: Optional[int],
                     user: Optional[models.User]) -> set[int]:
    """Every account this person holds, not just the one being bought under.

    The same widening as routers/bot.py's
    _ensure_one_time_package_not_reused, and for the same reason: a limit
    that looks at one username is dodged by making a second one.
    """
    ids: set[int] = set()
    if user is not None:
        ids.add(user.id)
    if telegram_id:
        ids.update(
            uid for (uid,) in
            db.query(models.User.id).filter(models.User.telegram_id == telegram_id).all()
        )
    return ids


def _has_bought_before(db: Session, account_ids: set[int]) -> bool:
    """Any purchase that was not itself a trial.

    Trials are excluded on purpose: someone whose only history is a trial
    has still never bought, and rule 1 already stops them taking a second
    one. Counting a trial as "has bought" would make rules 1 and 2 say the
    same thing twice.
    """
    if not account_ids:
        return False
    rows = (
        db.query(models.Purchase.package_id)
        .filter(models.Purchase.user_id.in_(account_ids))
        .all()
    )
    package_ids = {pid for (pid,) in rows if pid is not None}
    if not package_ids:
        # Purchases exist but name no package (legacy rows) - treat them as
        # real purchases. Guessing "probably a trial" here would hand a free
        # sample to an existing customer.
        return bool(rows)
    trial_ids = {
        pid for (pid,) in
        db.query(models.Package.id)
        .filter(models.Package.id.in_(package_ids), models.Package.is_trial.is_(True))
        .all()
    }
    return bool(package_ids - trial_ids)


def _today_bounds() -> tuple[dt.datetime, dt.datetime]:
    """UTC day. The cap is a safety valve against a link going around, not
    an accounting figure, so it does not need the panel's local-day
    handling - and a valve that is wrong by a few hours at the boundary is
    still a valve."""
    now = dt.datetime.utcnow()
    start = dt.datetime(now.year, now.month, now.day)
    return start, start + dt.timedelta(days=1)


def trials_given_today(db: Session, package: models.Package) -> int:
    start, end = _today_bounds()
    return (
        db.query(models.Purchase)
        .filter(
            models.Purchase.package_id == package.id,
            models.Purchase.created_at >= start,
            models.Purchase.created_at < end,
        )
        .count()
    )


def ensure_allowed(
    db: Session,
    package: Optional[models.Package],
    *,
    user: Optional[models.User] = None,
    telegram_id: Optional[int] = None,
) -> None:
    """Raises 403 with the customer's own answer, or returns quietly.

    403 rather than 400: this is "you may not", not "your request was
    malformed", and the wording has to be something a customer reads
    without feeling accused - most people hitting this are not farming,
    they have simply already had their sample.
    """
    if not is_trial(package):
        return

    # Rule 0, checked at the moment it costs money rather than only when
    # the package was saved.
    #
    # ensure_within_limits runs on create and update, which protects every
    # trial made from now on and NOT the one already sitting in the
    # database - the reported case was a trial already saved as «نامحدود /
    # بدون انقضا» before the limits existed, live and being given away. A
    # guard that only runs at configuration time cannot reach it.
    #
    # Refused, not clamped: silently handing out something different from
    # what the package says would leave the admin believing their unlimited
    # trial works, and the customer with a service nobody promised them.
    owner = db.get(models.AdminUser, package.owner_admin_id) if package.owner_admin_id else None
    if owner is not None and not owner.is_superadmin:
        try:
            ensure_within_limits(
                owner, quota_gb=package.quota_gb,
                duration_days=package.duration_days, price=package.price,
            )
        except HTTPException:
            raise HTTPException(
                403,
                "سرویس تست فعلاً در دسترس نیست - با پشتیبانی تماس بگیرید.",
            ) from None

    account_ids = _account_ids_for(db, telegram_id, user)

    # Rule 1, restated here rather than left to one_time_per_user: a trial
    # is once-only whether or not the admin remembered to tick that box.
    if account_ids:
        already = (
            db.query(models.Purchase)
            .filter(
                models.Purchase.user_id.in_(account_ids),
                models.Purchase.package_id == package.id,
            )
            .first()
        )
        if already:
            raise HTTPException(403, "شما قبلاً از سرویس تست استفاده کرده‌اید.")

    # Rule 2.
    if _has_bought_before(db, account_ids):
        raise HTTPException(403, "سرویس تست فقط برای کسانی است که هنوز خریدی نکرده‌اند.")

    # Rule 3.
    cap = getattr(package, "trial_daily_cap", None)
    if cap and trials_given_today(db, package) >= cap:
        raise HTTPException(
            403, "ظرفیت سرویس تست برای امروز تکمیل شده - فردا دوباره امتحان کنید.",
        )
