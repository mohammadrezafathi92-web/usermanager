"""One finite, unambiguous reward destination (Receipt Void, section 14.1).

Zero is unlimited, not an empty allowance. Never convert it to a limit or
guess between purchases. Resolve only after sale topology has been flushed.
"""
from .. import models


def resolve_quota_reward_target(purchases_after, user_quota_after):
    if not purchases_after:
        return ("User", None) if int(user_quota_after or 0) > 0 else None
    if len(purchases_after) == 1:
        purchase = purchases_after[0]
        return ("Purchase", purchase) if int(purchase.quota_bytes or 0) > 0 else None
    return None


def destination(db, user):
    # Sessions deliberately use autoflush=False. Include a Purchase created
    # by this transaction, not a stale relationship cached before the sale.
    db.flush()
    purchases = db.query(models.Purchase).filter_by(user_id=user.id).all()
    resolved = resolve_quota_reward_target(purchases, user.total_quota_bytes)
    return (resolved[1] or user) if resolved else None


def quota(resource):
    return int(resource.quota_bytes if isinstance(resource, models.Purchase)
               else resource.total_quota_bytes or 0)


def grant(resource, amount):
    if resource is None or amount <= 0:
        return 0
    before = quota(resource)
    if before <= 0:
        return 0
    if isinstance(resource, models.Purchase):
        resource.quota_bytes = before + amount
    else:
        resource.total_quota_bytes = before + amount
    return amount
