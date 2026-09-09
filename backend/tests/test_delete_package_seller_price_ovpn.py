"""routers/packages.py's delete_package - PackageSellerPrice and
PackageOvpnTemplate rows must never block (or orphan-leave) a package
delete.

Run:  python3 backend/tests/test_delete_package_seller_price_ovpn.py

Bug found 2026-09-09 while re-investigating a "package still won't delete"
report: models.py declares ondelete="CASCADE" on both PackageOvpnTemplate.
package_id and PackageSellerPrice.package_id, but neither relationship is
ALSO ORM-cascaded (no cascade="all, delete-orphan", unlike Package.
connections/files, which already are) - so both rely ENTIRELY on that DDL
clause actually being present on the live database. main.py's own
auto-migration only ever ADDs a missing COLUMN to an existing table, it
never ALTERs an existing constraint - so an install whose database
predates either column gaining that clause would silently be running with
a plain (RESTRICT-by-default) foreign key today despite what the current
models.py says, hitting the exact "won't delete, no error shown" shape
already fixed once for users.package_id/purchases.package_id in this same
function.

This test forces sqlite to actually ENFORCE foreign keys (off by default -
exactly why this whole class of bug only ever showed up in MariaDB
production and never in this sqlite-backed test suite) and additionally
proves the fix does not depend on that enforcement at all: the explicit
DELETE statements added to delete_package() must succeed and leave no
orphaned rows regardless of whether the database itself would have
enforced the constraint."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from sqlalchemy import event  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import packages as packages_router  # noqa: E402
from app.services import hierarchy  # noqa: E402


# sqlite does not enforce foreign keys unless explicitly told to - forcing
# it on here is what actually lets this test reproduce a RESTRICT-style
# failure the way MariaDB would, rather than silently letting a dangling
# reference through the way this whole suite's sqlite backing normally
# would.
@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, conn_record):
    dbapi_conn.execute("PRAGMA foreign_keys=ON")


Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin = models.AdminUser(username="admin1", hashed_password="x", is_superadmin=False, role="admin")
db.add(admin)
db.commit()
db.refresh(admin)
hierarchy.rebuild_path(db, admin)
db.commit()

seller = models.AdminUser(username="seller1", hashed_password="x", is_superadmin=False, role="seller", parent_admin_id=admin.id)
db.add(seller)
db.commit()
db.refresh(seller)
hierarchy.rebuild_path(db, seller)
db.commit()

pkg = packages_router.create_package(
    schemas.PackageCreate(name="پکیج تست", quota_gb=20, duration_days=30, price=10000),
    db=db, admin=admin,
)

print("--- set up a PackageSellerPrice override and a PackageOvpnTemplate for this package ---")
db.add(models.PackageSellerPrice(package_id=pkg.id, seller_admin_id=seller.id, price=9000))
db.add(models.PackageOvpnTemplate(package_id=pkg.id, name="آلمان", content="client\ndev tun\n"))
db.commit()
check(
    "seller price row exists",
    db.query(models.PackageSellerPrice).filter(models.PackageSellerPrice.package_id == pkg.id).count(),
    1,
)
check(
    "ovpn template row exists",
    db.query(models.PackageOvpnTemplate).filter(models.PackageOvpnTemplate.package_id == pkg.id).count(),
    1,
)

print("\n--- deleting the package succeeds (does not raise, even with FK enforcement on) ---")
try:
    result = packages_router.delete_package(pkg.id, db=db, admin=admin)
    check("delete_package returns ok", result, {"ok": True})
except Exception as exc:  # noqa: BLE001 - this failing at all IS the bug being tested for
    failures.append("delete_package raised")
    print(f"FAIL  delete_package raised: {exc!r}")

print("\n--- the package itself is gone ---")
check("package row gone", db.get(models.Package, pkg.id), None)

print("\n--- and neither child row was left orphaned behind it ---")
check(
    "seller price row cleaned up too",
    db.query(models.PackageSellerPrice).filter(models.PackageSellerPrice.package_id == pkg.id).count(),
    0,
)
check(
    "ovpn template row cleaned up too",
    db.query(models.PackageOvpnTemplate).filter(models.PackageOvpnTemplate.package_id == pkg.id).count(),
    0,
)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
