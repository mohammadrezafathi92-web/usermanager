"""routers/packages.py's list_packages - models.Package.seller_visible.

Run:  python3 backend/tests/test_package_seller_visibility.py

Feature requested 2026-09-08: next to a package's existing "نمایش در پنل"
(enabled) and "نمایش در ربات" (bot_enabled) toggles, an Admin can now also
control whether their own level-3 Sellers see/use that package in the WEB
PANEL specifically (create-user/renew dropdowns, the Packages.jsx list
itself) - independent of `enabled`, which already governs those same
dropdowns for everyone including the owning Admin. Default True, so an
existing package's visibility to Sellers is completely unchanged unless an
Admin deliberately turns it off for one.

Covers: a superadmin/Admin sees every package regardless of seller_visible
(they need to manage the flag itself); a Seller never receives a
seller_visible=False package from the SAME endpoint their create/renew
dropdown reads from; an enabled=True, seller_visible=False package is
still fully usable by the owning Admin; and PackageUpdate can flip the
flag on an existing package."""
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


from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import packages as packages_router  # noqa: E402
from app.services import hierarchy  # noqa: E402

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

print("--- create_package defaults seller_visible to True (unchanged behaviour) ---")
pkg_visible = packages_router.create_package(
    schemas.PackageCreate(name="پکیج عمومی", quota_gb=20, duration_days=30, price=10000),
    db=db, admin=admin,
)
check("defaults to visible", pkg_visible.seller_visible, True)

print("\n--- creating with seller_visible=False persists it ---")
pkg_hidden = packages_router.create_package(
    schemas.PackageCreate(name="پکیج داخلی", quota_gb=20, duration_days=30, price=10000, seller_visible=False),
    db=db, admin=admin,
)
check("stored as hidden-from-sellers", pkg_hidden.seller_visible, False)

print("\n--- the OWNING ADMIN still sees both packages ---")
admin_view = packages_router.list_packages(db=db, admin=admin)
admin_ids_seen = {p.id for p in admin_view}
check("admin sees the visible one", pkg_visible.id in admin_ids_seen, True)
check("admin ALSO sees the hidden-from-sellers one (needs to manage it)", pkg_hidden.id in admin_ids_seen, True)

print("\n--- the SELLER only sees the visible one ---")
seller_view = packages_router.list_packages(db=db, admin=seller)
seller_ids_seen = {p.id for p in seller_view}
check("seller sees the visible package", pkg_visible.id in seller_ids_seen, True)
check("seller does NOT see the hidden one", pkg_hidden.id in seller_ids_seen, False)

print("\n--- update_package can flip the flag on an existing package ---")
packages_router.update_package(pkg_visible.id, schemas.PackageUpdate(seller_visible=False), db=db, admin=admin)
db.refresh(pkg_visible)
check("now hidden from sellers", pkg_visible.seller_visible, False)
seller_view2 = packages_router.list_packages(db=db, admin=seller)
check("...and the seller's list reflects it immediately", pkg_visible.id in {p.id for p in seller_view2}, False)

packages_router.update_package(pkg_visible.id, schemas.PackageUpdate(seller_visible=True), db=db, admin=admin)
db.refresh(pkg_visible)
seller_view3 = packages_router.list_packages(db=db, admin=seller)
check("flipping back to True restores seller visibility", pkg_visible.id in {p.id for p in seller_view3}, True)

print("\n--- seller_visible=False never touches `enabled` (still usable by the Admin) ---")
check("enabled is unaffected", pkg_hidden.enabled, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
