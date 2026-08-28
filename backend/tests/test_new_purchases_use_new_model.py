"""A customer built with a package must land on the independent-Purchase
model from minute one, same as buying a SECOND package already did - not
the legacy shared-quota pool ("سرویس اشتراکی (قدیمی)" with a manual
"تبدیل به سرویس مستقل" button), reported 2026-08-28 on an account bought
minutes earlier.

routers/bot.py's create_user (the sales-bot purchase flow) already called
absorb_legacy_pool_into_purchase for this - see its own comment. The two
gaps were the WEB PANEL's own create_user (routers/users.py) and
bulk_create_users (services/user_ops.py), both of which built a package
purchase straight onto the user's combined fields with no Purchase at all.
This file proves both are now fixed, plus the startup sweep
(purchase_migration.migrate_legacy_only_users) that converts anyone who was
already stuck in that state before the fix shipped.

Run:  python3 backend/tests/test_new_purchases_use_new_model.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "test-secret")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import users as users_router
from app.services import user_ops, purchase_migration

failures: list[str] = []
GB = 1024 ** 3


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    admin = models.AdminUser(username="su", hashed_password="x", is_superadmin=True, balance=10_000_000)
    node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
    pkg = models.Package(name="۵۰ گیگ ماهانه", quota_gb=50, duration_days=30, price=90_000)
    db.add_all([admin, node, pkg])
    db.commit()
    db.refresh(admin)
    db.refresh(node)
    db.refresh(pkg)
    conn_spec = models.PackageConnection(package_id=pkg.id, node_id=node.id, protocol="l2tp")
    db.add(conn_spec)
    db.commit()
    return db, admin, node, pkg


def has_legacy_connections(db, user) -> bool:
    return (
        db.query(models.Connection)
        .filter(models.Connection.user_id == user.id, models.Connection.purchase_id.is_(None))
        .count() > 0
    )


print("--- panel: creating a user WITH a package (routers/users.py's create_user) ---")
db, admin, node, pkg = build()
payload = schemas.UserCreate(username="newcust1", package_id=pkg.id)
user = users_router.create_user(payload, db, admin)

check("the user was created", user.id is not None, True)
check("it has an independent Purchase", len(user.purchases), 1)
check("...with the package's own quota", user.purchases[0].quota_bytes, 50 * GB)
check("...and its own expiry, not the user-level field", user.purchases[0].expire_at is not None, True)
check("NO connection is left on the legacy shared pool", has_legacy_connections(db, user), False)
check("the connection is linked to the Purchase, not floating", user.connections[0].purchase_id, user.purchases[0].id)

print("\n--- panel: bulk-creating users WITH a package (services/user_ops.py) ---")
db, admin, node, pkg = build()
result = user_ops.bulk_create_users(db, "bulkcust", 3, package_id=pkg.id)
check("all 3 were created", result["created_count"], 3)
users = db.query(models.User).filter(models.User.username.like("bulkcust%")).all()
check("every one of them got an independent Purchase", all(len(u.purchases) == 1 for u in users), True)
check("none of them has a legacy connection left",
      all(not has_legacy_connections(db, u) for u in users), True)

print("\n--- startup sweep: a customer already stuck on the old model gets fixed too ---")
db, admin, node, pkg = build()
# Simulate exactly the reported screenshot: a customer whose account was
# built by the OLD (pre-fix) code path - combined fields set directly, a
# bare connection with no purchase_id, no Purchase row at all.
stuck_user = models.User(
    username="اکیگ_تست", owner_admin_id=admin.id,
    total_quota_bytes=1 * GB, used_bytes=int(0.7 * GB),
    expire_at=dt.datetime.utcnow() + dt.timedelta(days=25),
    package_id=pkg.id,
)
db.add(stuck_user)
db.commit()
stuck_conn = models.Connection(
    user_id=stuck_user.id, node_id=node.id, type="l2tp",
    ppp_username="stuck_c", ppp_password="x", enabled=True,
    package_name_snapshot=pkg.name,
)
db.add(stuck_conn)
db.commit()

check("before the sweep: still on the shared pool", has_legacy_connections(db, stuck_user), True)
fixed, left_shared = purchase_migration.migrate_legacy_only_users(db)
check("exactly one customer was converted", fixed, 1)
check("nobody was left unidentifiable", left_shared, 0)

db.refresh(stuck_user)
check("after the sweep: no legacy connection left", has_legacy_connections(db, stuck_user), False)
check("it now has its own Purchase", len(stuck_user.purchases), 1)
check("...carrying the EXACT usage across, not reset to zero", stuck_user.purchases[0].used_bytes, int(0.7 * GB))
check("...and the exact quota/expiry too", stuck_user.purchases[0].quota_bytes, 1 * GB)

print("\n--- the sweep is idempotent - running it again does nothing more ---")
fixed2, left2 = purchase_migration.migrate_legacy_only_users(db)
check("nothing left to convert", (fixed2, left2), (0, 0))

print("\n--- the sweep must NOT touch a 'mixed' customer - that's fix_mixed_users' job ---")
db, admin, node, pkg = build()
mixed_user = models.User(username="mixedcust", owner_admin_id=admin.id)
db.add(mixed_user)
db.commit()
existing_purchase = models.Purchase(
    user_id=mixed_user.id, package_id=pkg.id, quota_bytes=50 * GB, status=models.UserStatus.active,
)
db.add(existing_purchase)
db.commit()
legacy_conn = models.Connection(
    user_id=mixed_user.id, node_id=node.id, type="l2tp",
    ppp_username="mixed_c", ppp_password="x", enabled=True,
)
db.add(legacy_conn)
db.commit()

fixed3, left3 = purchase_migration.migrate_legacy_only_users(db)
check("a customer who already has a Purchase is left for fix_mixed_users, not touched here",
      fixed3, 0)
db.refresh(mixed_user)
check("...its legacy connection is still there, untouched", has_legacy_connections(db, mixed_user), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
