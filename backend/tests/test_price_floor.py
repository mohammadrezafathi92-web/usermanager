"""routers/packages.py's _effective_package_cost/_check_price_floor - a
package's own retail `price` (set by its owning Admin) and a Seller's own
`my_price` resale override (set_my_package_price) must never go below the
package's effective cooperation cost.

Run:  python3 backend/tests/test_price_floor.py

Feature requested 2026-09-09: nothing previously stopped an Admin typing a
package's customer-facing price below its own cooperation_price (what
their Sellers pay for it), nor a Seller setting their own resale price
below that same cooperation_price - both are a guaranteed loss on every
sale, and previously only cooperation_price itself was floor-checked
(_check_cooperation_floor, against the OWNING Admin's own per-GB rate from
the superadmin - a layer higher up the chain).

The floor here is recomputed fresh from the owning Admin's per-GB rate
(admin_billing.minimum_cooperation_price) when they have one configured -
more reliable than trusting a flat cooperation_price that may have gone
stale - and falls back to the flat cooperation_price otherwise."""
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


from fastapi import HTTPException  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import packages as packages_router  # noqa: E402
from app.services import hierarchy  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

superadmin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, role="superadmin")
db.add(superadmin)
db.commit()
db.refresh(superadmin)
hierarchy.rebuild_path(db, superadmin)
db.commit()

# A flat-rate Admin (no per-GB wholesale rate) - floor should fall back to
# the package's own flat cooperation_price.
flat_admin = models.AdminUser(username="admin_flat", hashed_password="x", is_superadmin=False, role="admin", wholesale_price_per_gb=0)
# A metered Admin, billed 1000 toman/GB by the superadmin.
metered_admin = models.AdminUser(username="admin_metered", hashed_password="x", is_superadmin=False, role="admin", wholesale_price_per_gb=1000)
db.add_all([flat_admin, metered_admin])
db.commit()
db.refresh(flat_admin)
db.refresh(metered_admin)
hierarchy.rebuild_path(db, flat_admin)
hierarchy.rebuild_path(db, metered_admin)
db.commit()

seller = models.AdminUser(username="seller1", hashed_password="x", is_superadmin=False, role="seller", parent_admin_id=flat_admin.id)
db.add(seller)
db.commit()
db.refresh(seller)
hierarchy.rebuild_path(db, seller)
db.commit()

print("--- flat-rate admin: price below the flat cooperation_price is refused ---")
try:
    packages_router.create_package(
        schemas.PackageCreate(name="پکیج ۱", quota_gb=10, duration_days=30, price=4000, cooperation_price=5000),
        db=db, admin=flat_admin,
    )
    check("raises", False, True)
except HTTPException as exc:
    check("raises 400", exc.status_code, 400)

print("\n--- flat-rate admin: price EQUAL to cooperation_price is allowed (selling at cost is fine) ---")
pkg_flat = packages_router.create_package(
    schemas.PackageCreate(name="پکیج ۲", quota_gb=10, duration_days=30, price=5000, cooperation_price=5000),
    db=db, admin=flat_admin,
)
check("created", pkg_flat.price, 5000)

print("\n--- metered admin (1000/GB, quota 10 => real cost 10000): price below the real per-GB cost is refused even with a valid cooperation_price ---")
try:
    packages_router.create_package(
        schemas.PackageCreate(name="پکیج ۳", quota_gb=10, duration_days=30, price=9000, cooperation_price=10000),
        db=db, admin=metered_admin,
    )
    check("raises", False, True)
except HTTPException as exc:
    check("raises 400 (real per-GB cost, not the stored cooperation_price)", exc.status_code, 400)

print("\n--- metered admin: price at the REAL per-GB cost (10000) is allowed ---")
pkg_metered = packages_router.create_package(
    schemas.PackageCreate(name="پکیج ۴", quota_gb=10, duration_days=30, price=10000, cooperation_price=10000),
    db=db, admin=metered_admin,
)
check("created", pkg_metered.price, 10000)

print("\n--- update_package: dropping price below the (unchanged) cooperation_price is refused ---")
try:
    packages_router.update_package(pkg_flat.id, schemas.PackageUpdate(price=1000), db=db, admin=flat_admin)
    check("raises", False, True)
except HTTPException as exc:
    check("raises 400", exc.status_code, 400)

print("\n--- no cooperation_price and no per-GB rate at all -> no floor, any price allowed ---")
pkg_free = packages_router.create_package(
    schemas.PackageCreate(name="پکیج آزاد", quota_gb=10, duration_days=30, price=0),
    db=db, admin=flat_admin,
)
check("created with price 0, no error", pkg_free.price, 0)

print("\n--- seller's OWN resale price (my_price) below the package's cooperation_price is refused ---")
try:
    packages_router.set_my_package_price(
        pkg_flat.id, schemas.SellerPackagePriceUpdate(price=3000), db=db, admin=seller,
    )
    check("raises", False, True)
except HTTPException as exc:
    check("raises 400", exc.status_code, 400)

print("\n--- seller's resale price AT the cooperation_price floor is allowed ---")
out = packages_router.set_my_package_price(pkg_flat.id, schemas.SellerPackagePriceUpdate(price=5000), db=db, admin=seller)
check("saved", out.my_price, 5000)

print("\n--- clearing the seller's own price override (price=None) is always allowed, no floor check ---")
out2 = packages_router.set_my_package_price(pkg_flat.id, schemas.SellerPackagePriceUpdate(price=None), db=db, admin=seller)
check("cleared", out2.my_price, None)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
