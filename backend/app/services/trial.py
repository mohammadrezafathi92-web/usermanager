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


def is_trial(package: Optional[models.Package]) -> bool:
    return bool(package is not None and getattr(package, "is_trial", False))


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
