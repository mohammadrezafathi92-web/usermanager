"""First-successful-connection expiry helpers for user and purchase plans."""
from __future__ import annotations

import datetime as dt

from sqlalchemy import update
from sqlalchemy.orm import Session

from .. import models


def package_expiry(package: models.Package, now: dt.datetime | None = None) -> tuple[dt.datetime | None, int | None]:
    """Return the absolute expiry and pending first-use duration for a package."""
    days = int(package.duration_days or 0)
    if days <= 0:
        return None, None
    if package.starts_on_first_connection:
        return None, days
    return (now or dt.datetime.utcnow()) + dt.timedelta(days=days), None


def activate_expiry(db: Session, resource, now: dt.datetime | None = None) -> bool:
    """Atomically anchor a pending User/Purchase expiry once, never extending it.

    Compare-and-set makes concurrent first connections harmless: only one
    request can replace the pending duration, and later requests cannot move
    the resulting expiry forward.
    """
    if not isinstance(resource, (models.User, models.Purchase)):
        return False
    days = int(resource.expire_days_after_first_use or 0)
    if days <= 0 or resource.expire_at is not None:
        return False
    now = now or dt.datetime.utcnow()
    result = db.execute(
        update(type(resource))
        .where(
            type(resource).id == resource.id,
            type(resource).expire_at.is_(None),
            type(resource).expire_days_after_first_use == days,
        )
        .values(expire_at=now + dt.timedelta(days=days), expire_days_after_first_use=None)
    )
    db.expire(resource, ["expire_at", "expire_days_after_first_use"])
    return result.rowcount == 1


def activate_connection_expiry(db: Session, connection, now: dt.datetime | None = None) -> bool:
    """Start the expiry clock for the allotment that owns this connection."""
    if connection.purchase_id is not None:
        purchase = db.get(models.Purchase, connection.purchase_id)
        return activate_expiry(db, purchase, now) if purchase is not None else False
    user = db.get(models.User, connection.user_id)
    return activate_expiry(db, user, now) if user is not None else False
