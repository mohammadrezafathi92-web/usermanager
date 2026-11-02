"""A cooperation price for ONE admin/seller on ONE package.

Run:  python3 backend/tests/test_admin_package_price.py

Owner decision (2026-11-02): each admin or seller may get their own
cooperation price per package. Order, most specific first:
  1. this account's own price for this package (AdminPackagePrice)
  2. the account's per-GB rate (quota x wholesale_price_per_gb)
  3. Package.cooperation_price
  4. Package.price
A renewal with a package costs the same as buying it. The price is set
from above only (superadmin -> any admin/seller, level-2 admin -> own
sellers on own packages) and an admin can never set a seller price below
what that package costs the admin.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "admin-package-price-test-only-0123456789")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import admin_billing

failures: list[str] = []


def check(label, got, expected):
    if callable(got):
        try:
            got = got()
        except HTTPException as exc:
            got = f"HTTP {exc.status_code}"
        except Exception as exc:  # noqa: BLE001 - a missing API is one FAIL, not an abort
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, tree_path="/1/")
db.add(root); db.flush()
reseller = models.AdminUser(username="reseller", hashed_password="x", role="admin", balance=10_000_000,
                            wholesale_price_per_gb=1_000)
db.add(reseller); db.flush()
seller = models.AdminUser(username="seller", hashed_password="x", role="seller", parent_admin_id=reseller.id,
                          balance=10_000_000)
other_seller = models.AdminUser(username="seller2", hashed_password="x", role="seller", parent_admin_id=root.id,
                                balance=10_000_000)
db.add_all([seller, other_seller]); db.flush()
global_pkg = models.Package(name="global 20g", quota_gb=20, duration_days=30, price=300_000, cooperation_price=150_000)
unlimited = models.Package(name="global unlimited", quota_gb=0, duration_days=30, price=500_000, cooperation_price=200_000)
own_pkg = models.Package(name="reseller 10g", owner_admin_id=reseller.id, quota_gb=10, duration_days=30,
                         price=200_000, cooperation_price=40_000)
own_20 = models.Package(name="reseller 20g", owner_admin_id=reseller.id, quota_gb=20, duration_days=30,
                        price=300_000, cooperation_price=1)
own_unlimited = models.Package(name="reseller unlimited", owner_admin_id=reseller.id, quota_gb=0, duration_days=30,
                               price=500_000, cooperation_price=1)
db.add_all([global_pkg, unlimited, own_pkg, own_20, own_unlimited]); db.commit()


def price(admin, package):
    return lambda: admin_billing.unit_price(admin, package)


def source(admin, package):
    return lambda: admin_billing.price_source(admin, package)


def set_price(actor, target, package, value):
    return lambda: admin_billing.set_package_price(db, actor, target, package, value)


print("--- without an own price nothing changes ---")
check("rate admin: quota x rate", price(reseller, own_20), 20_000)
check("...source says per-GB rate", source(reseller, own_20), (20_000, "per_gb_rate"))
check("rate admin on unlimited: still refused", price(reseller, own_unlimited), "HTTP 400")
check("seller without rate: package cooperation price", price(other_seller, global_pkg), 150_000)
check("...source says package cooperation price", source(other_seller, global_pkg), (150_000, "package_cooperation"))

print("\n--- superadmin sets own prices ---")
check("superadmin sets the reseller's price on its package", set_price(root, reseller, own_20, 90_000), 90_000)
check("own price wins over the per-GB rate", price(reseller, own_20), 90_000)
check("...source says own price", source(reseller, own_20), (90_000, "own_price"))
check("own price makes an unlimited package sellable for a rate admin",
      lambda: (set_price(root, reseller, own_unlimited, 120_000)(), admin_billing.unit_price(reseller, own_unlimited))[1], 120_000)
check("superadmin cannot price a package the account cannot use", set_price(root, reseller, global_pkg, 1), "HTTP 403")
check("another account is unaffected", price(other_seller, global_pkg), 150_000)
check("superadmin sets a seller's price", set_price(root, other_seller, global_pkg, 130_000), 130_000)
check("...and it applies", price(other_seller, global_pkg), 130_000)
check("updating replaces, never duplicates",
      lambda: (set_price(root, other_seller, global_pkg, 125_000)(),
               db.query(models.AdminPackagePrice).filter_by(admin_id=other_seller.id, package_id=global_pkg.id).count(),
               admin_billing.unit_price(other_seller, global_pkg))[1:], (1, 125_000))
check("clearing (None) falls back to the next rule",
      lambda: (set_price(root, other_seller, global_pkg, None)(), admin_billing.unit_price(other_seller, global_pkg))[1],
      150_000)

print("\n--- validation ---")
for label, bad in (("negative", -1), ("bool", True), ("float", 1.5), ("string", "100")):
    check(f"{label} price refused", set_price(root, other_seller, global_pkg, bad), "HTTP 400")
check("zero is a real price (free), not 'unset'",
      lambda: (set_price(root, other_seller, unlimited, 0)(), admin_billing.price_source(other_seller, unlimited))[1],
      (0, "own_price"))
check("the superadmin cannot price itself", set_price(root, root, global_pkg, 1), "HTTP 400")

print("\n--- a level-2 admin prices only its own sellers, on its own packages ---")
check("admin sets its seller's price on its own package", set_price(reseller, seller, own_pkg, 50_000), 50_000)
check("...seller pays it", price(seller, own_pkg), 50_000)
check("admin cannot price a seller it does not own", set_price(reseller, other_seller, own_pkg, 50_000), "HTTP 403")
check("admin cannot price itself", set_price(reseller, reseller, own_pkg, 1), "HTTP 403")
check("admin cannot price a package it does not own", set_price(reseller, seller, global_pkg, 500_000), "HTTP 403")
check("admin cannot sell to its seller below its own cost (10GB x 1,000)",
      set_price(reseller, seller, own_pkg, 9_999), "HTTP 400")
check("...at cost is allowed", set_price(reseller, seller, own_pkg, 10_000), 10_000)
check("a seller cannot set prices", set_price(seller, seller, own_pkg, 1), "HTTP 403")

print("\n--- every charge path uses it ---")
before = db.get(models.AdminUser, reseller.id).balance
check("charge_for_package debits the own price",
      lambda: (admin_billing.charge_for_package(db, reseller, own_20), before - db.get(models.AdminUser, reseller.id).balance)[1],
      90_000)
check("package_cost agrees", lambda: admin_billing.package_cost(reseller, own_20), 90_000)
check("a renewal with the package costs the same as buying it",
      lambda: admin_billing.renewal_cost(reseller, own_20, 0), 90_000)
mid = db.get(models.AdminUser, reseller.id).balance
check("refund gives back the same own price",
      lambda: (admin_billing.refund_for_package(db, reseller, own_20, 1), db.get(models.AdminUser, reseller.id).balance - mid)[1],
      90_000)

print("\n--- the listing an operator sees ---")
rows = lambda: {r["package_id"]: (r["price"], r["source"], r["own_price"]) for r in admin_billing.package_price_rows(db, seller)}
check("seller's listing: own price where set, inherited elsewhere", rows,
      {own_pkg.id: (10_000, "own_price", 10_000), own_20.id: (1, "package_cooperation", None),
       own_unlimited.id: (1, "package_cooperation", None)})
check("deleting the package removes its own prices", lambda: (
    db.delete(db.get(models.Package, own_pkg.id)), db.commit(),
    db.query(models.AdminPackagePrice).filter_by(package_id=own_pkg.id).count())[2], 0)

print("\n--- the HTTP API: scope and strict body ---")
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.database import get_db
from app import deps
from app.routers import admins as admins_router

app = FastAPI()
app.include_router(admins_router.router)
app.dependency_overrides[get_db] = lambda: db
acting = {"who": root}
app.dependency_overrides[deps.get_current_admin] = lambda: db.get(models.AdminUser, acting["who"].id)
client = TestClient(app)
other_seller_id, global_id = other_seller.id, global_pkg.id
check("superadmin lists a seller's packages",
      lambda: sorted(r["package_id"] for r in client.get(f"/api/admins/{other_seller_id}/package-prices").json()),
      sorted([global_id, unlimited.id]))
check("superadmin sets a price over HTTP", lambda: client.put(
    f"/api/admins/{other_seller_id}/package-prices/{global_id}", json={"cooperation_price": 111_000}).json()["source"],
      "own_price")
for label, body in (("bool", {"cooperation_price": True}), ("float", {"cooperation_price": 1.0}),
                    ("string", {"cooperation_price": "5"}), ("missing", {}), ("extra key", {"cooperation_price": 1, "x": 1})):
    check(f"HTTP {label} body refused with 422", lambda body=body: client.put(
        f"/api/admins/{other_seller_id}/package-prices/{global_id}", json=body).status_code, 422)
check("HTTP null clears it", lambda: client.put(
    f"/api/admins/{other_seller_id}/package-prices/{global_id}", json={"cooperation_price": None}).json()["source"],
      "package_cooperation")
acting["who"] = reseller
check("a level-2 admin cannot list another tree's seller",
      lambda: client.get(f"/api/admins/{other_seller_id}/package-prices").status_code, 403)
check("a level-2 admin cannot set another tree's seller",
      lambda: client.put(f"/api/admins/{other_seller_id}/package-prices/{global_id}",
                         json={"cooperation_price": 1}).status_code, 403)
acting["who"] = seller
check("a seller cannot use the admins API at all",
      lambda: client.get(f"/api/admins/{seller.id}/package-prices").status_code, 403)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall admin package price checks passed")
