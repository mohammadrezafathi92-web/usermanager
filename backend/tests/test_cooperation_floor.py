"""An Admin cannot sell a package to their own Sellers below what it costs
them.

Run:  python3 backend/tests/test_cooperation_floor.py

Package.cooperation_price is what an Admin's SELLERS pay, and the Admin
sets it themselves. Nothing stopped them putting it under their own per-GB
cost - which loses money on every seller sale, quietly, on every one.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.services import admin_billing, hierarchy
from app.routers import packages as packages_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def admin(**kw):
    return models.AdminUser(
        username=kw.get("username", "a1"), hashed_password="x",
        is_superadmin=kw.get("su", False), role=kw.get("role", "admin"),
        wholesale_price_per_gb=kw.get("rate", 0),
    )


print("--- the floor itself ---")
a = admin(rate=1_000)
check("50GB at 1,000/GB", admin_billing.minimum_cooperation_price(a, 50), 50_000)
check("10GB", admin_billing.minimum_cooperation_price(a, 10), 10_000)
check("fractional quota rounds", admin_billing.minimum_cooperation_price(a, 2.5), 2_500)

check("no rate means no floor",
      admin_billing.minimum_cooperation_price(admin(rate=0), 50), None)
check("a superadmin has no floor - they set the prices",
      admin_billing.minimum_cooperation_price(admin(su=True, rate=1_000), 50), None)
check("an unlimited package has no per-GB floor",
      admin_billing.minimum_cooperation_price(a, 0), None)

print("\n--- what the package form accepts ---")


def save(admin_obj, quota_gb, coop):
    try:
        packages_router._check_cooperation_floor(admin_obj, quota_gb, coop)
        return True, None
    except HTTPException as exc:
        return False, str(exc.detail)


check("above cost is fine", save(a, 50, 70_000), (True, None))
check("exactly at cost is allowed - selling on at cost is a choice",
      save(a, 50, 50_000), (True, None))

ok, detail = save(a, 50, 49_999)
check("one toman below cost is refused", ok, False)
check("...the message names the floor", "50,000" in detail, True)
check("...and the rate behind it", "1,000" in detail, True)

ok, _ = save(a, 50, 0)
check("zero is refused when a rate is set", ok, False)

check("not setting a cooperation price at all is allowed",
      save(a, 50, None), (True, None))
check("an admin with no rate can price freely",
      save(admin(rate=0), 50, 1), (True, None))
check("a superadmin can price freely",
      save(admin(su=True, rate=1_000), 50, 1), (True, None))
check("an unlimited package is not blocked here",
      save(a, 0, 1), (True, None))

print("\n--- the endpoints apply it ---")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
lvl2 = admin(rate=1_000, username="reseller")
db.add(lvl2)
db.commit()
db.refresh(lvl2)
hierarchy.rebuild_path(db, lvl2)
db.commit()


def create(quota_gb, coop):
    payload = schemas.PackageCreate(
        name="p", quota_gb=quota_gb, duration_days=30, price=100_000,
        cooperation_price=coop, connections=[], ovpn_templates=[],
    )
    try:
        return True, packages_router.create_package(payload, db=db, admin=lvl2)
    except HTTPException as exc:
        db.rollback()
        return False, str(exc.detail)


ok, _ = create(50, 20_000)
check("creating a below-cost package is refused", ok, False)
check("and nothing was written", db.query(models.Package).count(), 0)

ok, _ = create(50, 60_000)
check("creating an above-cost package works", ok, True)
check("...and it exists", db.query(models.Package).count(), 1)

print("\n--- editing is judged on the RESULT, not just what changed ---")
pkg = db.query(models.Package).one()


def update(**fields):
    payload = schemas.PackageUpdate(**fields)
    try:
        packages_router.update_package(pkg.id, payload, db=db, admin=lvl2)
        return True, None
    except HTTPException as exc:
        db.rollback()
        return False, str(exc.detail)


# 60,000 was fine for 50GB. Raising quota to 100GB makes the cost 100,000,
# so the untouched price is now below it - the classic way a floor gets
# bypassed when only the submitted fields are checked.
ok, _ = update(quota_gb=100)
check("raising quota under a fixed price is refused", ok, False)
db.refresh(pkg)
check("the package is unchanged", (pkg.quota_gb, pkg.cooperation_price), (50, 60_000))

check("raising both together works", update(quota_gb=100, cooperation_price=110_000), (True, None))
db.refresh(pkg)
check("...and applied", (pkg.quota_gb, pkg.cooperation_price), (100, 110_000))

check("lowering the price alone, below the new cost, is refused",
      update(cooperation_price=50_000)[0], False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
