"""One-time conversion of legacy shared-pool connection groups into real,
independently-enforced Purchases (models.Purchase) - agreed with the panel
owner 2026-08-09 after the tg266249955 case: a "20GB/1month" package had
been silently pooled into the user's combined 300GB quota by the old bot
purchase flow, letting it burn 24GB with nothing stopping it, while the
user-detail page showed one meaningless aggregate bar over three unrelated
services (two of them unlimited).

Rules (deliberately conservative - NOTHING is guessed):

1. A user's legacy (purchase_id IS NULL) connections are grouped by
   Connection.purchase_batch; batch-less connections all form one "base"
   group (they're the original create-user-with-package service).

2. Exactly ONE group -> that group IS the user's whole pool: the Purchase
   copies the user's combined quota/used/expiry/reserved-renewal fields
   1:1. Zero behavioral change, just the correct structure.

3. MULTIPLE groups -> each group whose package is still identifiable (via
   Connection.package_name_snapshot -> Package by name, or the user's own
   package_id for the base group) gets a Purchase with THAT package's
   quota, expiry = the group's purchase date + package duration, and
   used = the sum of its own connections' total_bytes - which is exact,
   not reconstructed, because per-connection usage was always tracked.
   Groups whose package can't be identified are left on the shared pool
   (the UserDetail page keeps showing them under "سرویس اشتراکی" with a
   manual-convert action instead of us inventing numbers).

4. The user-level reserved renewal, if any, is attached to the single
   converted purchase that is actually exhausted/expired (that's the
   service the customer paid to renew); if that's ambiguous (zero or 2+
   exhausted) it stays at user level for the admin to place by hand.

5. Statuses start as "active": the very next quota_manager poll enforces
   each new Purchase's own limits, which also auto-activates a reserved
   renewal on an exhausted service (so the 24/20GB customer flips straight
   onto their already-paid 320GB instead of experiencing a cut-off).

Guarded by PanelSettings.legacy_purchases_migrated so it runs exactly once
(main.py's on_startup)."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session, selectinload

from .. import models
from .user_ops import gb_to_bytes

logger = logging.getLogger("purchase_migration")

BASE_GROUP_KEY = "__base__"


def _group_legacy_connections(user: models.User) -> dict[str, list[models.Connection]]:
    groups: dict[str, list[models.Connection]] = {}
    for conn in user.connections:
        if conn.purchase_id is not None:
            continue
        key = conn.purchase_batch or BASE_GROUP_KEY
        groups.setdefault(key, []).append(conn)
    return groups


def _resolve_package(db: Session, user: models.User, key: str, conns: list[models.Connection]) -> models.Package | None:
    name = next((c.package_name_snapshot for c in conns if c.package_name_snapshot), None)
    if name:
        pkg = db.query(models.Package).filter(models.Package.name == name).first()
        if pkg is not None:
            return pkg
    if key == BASE_GROUP_KEY and user.package_id:
        return db.get(models.Package, user.package_id)
    return None


def _make_purchase(db: Session, user: models.User, conns: list[models.Connection], **fields) -> models.Purchase:
    purchase = models.Purchase(user_id=user.id, status=models.UserStatus.active, **fields)
    db.add(purchase)
    db.flush()  # assigns purchase.id for the FK links below
    for conn in conns:
        conn.purchase_id = purchase.id
    return purchase


def migrate_user(db: Session, user: models.User) -> tuple[int, int]:
    """Converts one user's legacy groups. Returns (converted, skipped)."""
    groups = _group_legacy_connections(user)
    if not groups:
        return 0, 0

    now = dt.datetime.utcnow()
    converted: list[models.Purchase] = []
    skipped = 0

    if len(groups) == 1:
        # Rule 2: the whole pool belongs to this one group - copy it 1:1.
        key, conns = next(iter(groups.items()))
        pkg = _resolve_package(db, user, key, conns)
        purchase = _make_purchase(
            db, user, conns,
            package_id=pkg.id if pkg else user.package_id,
            package_name_snapshot=(pkg.name if pkg else None) or next((c.package_name_snapshot for c in conns), None),
            quota_bytes=user.total_quota_bytes or 0,
            used_bytes=user.used_bytes or 0,
            expire_at=user.expire_at,
            expire_days_after_first_use=user.expire_days_after_first_use,
            reserved_quota_bytes=user.reserved_quota_bytes,
            reserved_duration_days=user.reserved_duration_days,
            reserved_created_at=user.reserved_created_at,
            created_at=min((c.created_at for c in conns if c.created_at), default=user.created_at),
        )
        converted.append(purchase)
        # The reservation now lives on the purchase - never in both places.
        user.reserved_quota_bytes = None
        user.reserved_duration_days = None
        user.reserved_created_at = None
        return 1, 0

    # Rule 3: several distinct purchases were pooled - split by package.
    for key, conns in groups.items():
        pkg = _resolve_package(db, user, key, conns)
        if pkg is None:
            skipped += 1
            continue
        group_created = min((c.created_at for c in conns if c.created_at), default=now)
        purchase = _make_purchase(
            db, user, conns,
            package_id=pkg.id,
            package_name_snapshot=pkg.name,
            quota_bytes=gb_to_bytes(pkg.quota_gb) if pkg.quota_gb else 0,
            used_bytes=sum(c.total_bytes or 0 for c in conns),
            expire_at=(group_created + dt.timedelta(days=pkg.duration_days)) if pkg.duration_days else None,
            created_at=group_created,
        )
        converted.append(purchase)

    # Rule 4: place the user-level reserved renewal on the one service
    # that's actually finished - the thing it was bought to renew.
    if (user.reserved_quota_bytes or user.reserved_duration_days) and converted:
        exhausted = [
            p for p in converted
            if (p.quota_bytes and (p.used_bytes or 0) >= p.quota_bytes) or (p.expire_at and p.expire_at <= now)
        ]
        if len(exhausted) == 1:
            target = exhausted[0]
            target.reserved_quota_bytes = user.reserved_quota_bytes
            target.reserved_duration_days = user.reserved_duration_days
            target.reserved_created_at = user.reserved_created_at or now
            user.reserved_quota_bytes = None
            user.reserved_duration_days = None
            user.reserved_created_at = None

    return len(converted), skipped


def fix_mixed_users(db: Session) -> int:
    """Repairs customers left in the "mixed" state - some connections on an
    independent Purchase, some still on the shared user pool. See
    user_ops.absorb_legacy_pool_into_purchase for why that state is a trap
    (renewals go to the Purchase; the leftover connections stay pinned to a
    frozen user-level expiry and get cut off with no way to renew them).

    Runs on every startup, not once: it's a cheap query, and it also
    cleans up after any code path that might still produce the state.
    Returns how many customers were repaired."""
    fixed = 0
    # Eager-loaded: the two guards below read `.purchases` and `.connections`
    # on every single customer, so the plain query cost 1 + 2N statements
    # (measured: 1,219 for 609 users) on every backend start just to decide
    # that almost nobody needs repairing.
    users = (
        db.query(models.User)
        .options(selectinload(models.User.purchases), selectinload(models.User.connections))
        .all()
    )
    for user in users:
        if not user.purchases:
            continue  # legacy-only customer - their pool is still live, leave it alone
        if not any(c.purchase_id is None for c in user.connections):
            continue  # already fully on the per-service model
        try:
            if user_ops_absorb(db, user) is not None:
                fixed += 1
        except Exception:
            logger.exception("failed to absorb the legacy pool for user %s", user.username)
            db.rollback()
    if fixed:
        db.commit()
        logger.info("%s کاربر ترکیبی اصلاح شد (اتصال‌های قدیمی به سرویس مستقل منتقل شدند)", fixed)
    return fixed


def user_ops_absorb(db: Session, user: models.User):
    # Imported lazily - user_ops imports plenty at module load, and this
    # module is itself imported from main.py's startup path.
    from .user_ops import absorb_legacy_pool_into_purchase

    return absorb_legacy_pool_into_purchase(db, user)


def migrate_if_needed(db: Session) -> tuple[int, int]:
    """Entry point called from main.py's on_startup. Returns totals of
    (purchases created, groups left shared)."""
    settings = db.query(models.PanelSettings).first()
    if settings is None or settings.legacy_purchases_migrated:
        return 0, 0

    total_converted = 0
    total_skipped = 0
    users = (
        db.query(models.User)
        .options(selectinload(models.User.connections))
        .all()
    )
    for user in users:
        try:
            converted, skipped = migrate_user(db, user)
            total_converted += converted
            total_skipped += skipped
        except Exception:
            logger.exception("migration failed for user %s - leaving them on the shared pool", user.username)
            db.rollback()

    settings.legacy_purchases_migrated = True
    db.commit()
    logger.info(
        "مهاجرت سرویس‌های قدیمی: %s سرویس مستقل ساخته شد، %s گروه (پکیج ناشناس) اشتراکی ماند",
        total_converted, total_skipped,
    )
    return total_converted, total_skipped
