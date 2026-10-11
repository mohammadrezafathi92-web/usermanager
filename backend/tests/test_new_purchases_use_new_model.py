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
from fastapi import HTTPException

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

print("\n--- a manual connection is linked in its first commit ---")
db, admin, node, pkg = build()
manual = models.User(username="manual", owner_admin_id=admin.id,
                     total_quota_bytes=7 * GB, used_bytes=GB)
db.add(manual)
db.commit()
first = user_ops.provision_openvpn(db, manual, node)
second = user_ops.provision_l2tp(db, manual, node)
check("both manual credentials have a Purchase", bool(first.purchase_id and second.purchase_id), True)
check("no manual credential uses the old pool", has_legacy_connections(db, manual), False)
check("the original allowance was not multiplied", sum(p.quota_bytes for p in manual.purchases), 7 * GB)
extra = models.Purchase(user_id=manual.id, quota_bytes=2 * GB, status=models.UserStatus.active)
db.add(extra)
db.commit()
try:
    users_router.add_openvpn_connection(manual.id, schemas.ConnectionCreateOpenvpn(node_id=node.id), db, admin)
    check("multiple services require an explicit destination", False, True)
except HTTPException as exc:
    check("multiple services require an explicit destination", exc.status_code, 409)
db.rollback()
selected = users_router.add_openvpn_connection(
    manual.id, schemas.ConnectionCreateOpenvpn(node_id=node.id, purchase_id=extra.id), db, admin)
check("explicit destination owns the new connection", selected.purchase_id, extra.id)
check("manual multi-service addition still leaves no legacy row", has_legacy_connections(db, manual), False)

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
    reserved_quota_bytes=2 * GB, reserved_duration_days=30, reserved_package_id=pkg.id,
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
check("reserved renewal package survives the move", stuck_user.purchases[0].reserved_package_id, pkg.id)
check("reserved renewal no longer lives on User", stuck_user.reserved_package_id, None)

print("\n--- the sweep is idempotent - running it again does nothing more ---")
fixed2, left2 = purchase_migration.migrate_legacy_only_users(db)
check("nothing left to convert", (fixed2, left2), (0, 0))

print("\n--- unknown package history remains one exact pool, never legacy ---")
db, admin, node, pkg = build()
pooled = models.User(
    username="ambiguous", owner_admin_id=admin.id,
    total_quota_bytes=17 * GB, used_bytes=3 * GB,
    expire_at=dt.datetime.utcnow() + dt.timedelta(days=9),
)
db.add(pooled)
db.flush()
for batch in ("known", "unknown"):
    db.add(models.Connection(
        user_id=pooled.id, node_id=node.id, type="l2tp",
        ppp_username=f"ambiguous_{batch}", ppp_password="x",
        purchase_batch=batch,
        package_name_snapshot=pkg.name if batch == "known" else None,
    ))
db.commit()
fixed, left = purchase_migration.migrate_legacy_only_users(db)
db.expire_all()
pooled = db.query(models.User).filter_by(username="ambiguous").one()
check("ambiguous groups converted without leftovers", (fixed, left), (1, 0))
check("one shared entitlement is preserved", len(pooled.purchases), 1)
check("exact quota and usage survive", (pooled.purchases[0].quota_bytes, pooled.purchases[0].used_bytes),
      (17 * GB, 3 * GB))
check("both connections belong to it", {c.purchase_id for c in pooled.connections}, {pooled.purchases[0].id})

print("\n--- an import commits only linked credentials ---")
db, admin, node, pkg = build()
imported = models.User(username="imported", owner_admin_id=admin.id,
                       total_quota_bytes=4 * GB, used_bytes=GB)
db.add(imported)
db.flush()
for index in range(2):
    db.add(models.Connection(user_id=imported.id, node_id=node.id, type="l2tp",
                             ppp_username=f"imported_{index}", ppp_password="x"))
user_ops._finish_imported_purchases(db, {imported.id})
db.commit()
check("import has no old-model connection", has_legacy_connections(db, imported), False)
check("import preserves a single allowance", len(imported.purchases), 1)
check("import preserves usage", imported.purchases[0].used_bytes, GB)

print("\n--- conversion never re-enables a manually disabled account ---")
db, admin, node, pkg = build()
disabled = models.User(username="disabled_old", owner_admin_id=admin.id,
                       status=models.UserStatus.disabled, total_quota_bytes=GB)
db.add(disabled)
db.flush()
db.add(models.Connection(user_id=disabled.id, node_id=node.id, type="l2tp",
                         ppp_username="disabled_old", ppp_password="x", enabled=False))
db.commit()
purchase_migration.migrate_legacy_only_users(db)
db.expire_all()
check("disabled service remains disabled", disabled.purchases[0].status, models.UserStatus.disabled)
check("disabled service has no old-model connection", has_legacy_connections(db, disabled), False)

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
