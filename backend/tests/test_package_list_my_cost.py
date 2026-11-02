"""The package list tells the caller what THEY pay for each package.

Run:  python3 backend/tests/test_package_list_my_cost.py

Bug (2026-11-03): a seller with an own cooperation price saw the package's
sale price in the panel's create-user form ("X will be deducted from your
balance") although the charge itself used the own price. The list now
carries my_cost / my_cost_source - exactly what admin_billing would charge.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "package-list-my-cost-test-only-0123456789")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deps, models
from app.database import get_db
from app.routers import packages as packages_router

failures: list[str] = []


def check(label, got, expected):
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001 - a missing field is one FAIL, not an abort
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
seller = models.AdminUser(username="seller", hashed_password="x", role="seller", parent_admin_id=root.id,
                          balance=10_000_000)
plain = models.AdminUser(username="plain", hashed_password="x", role="seller", parent_admin_id=root.id,
                         balance=10_000_000)
usage = models.AdminUser(username="usage", hashed_password="x", role="seller", parent_admin_id=root.id,
                         billing_mode="usage")
per_gb = models.AdminUser(username="pergb", hashed_password="x", role="seller", parent_admin_id=root.id,
                          wholesale_price_per_gb=1_000)
db.add_all([seller, plain, usage, per_gb]); db.flush()
pkg = models.Package(name="50g", quota_gb=50, duration_days=30, price=300_000, cooperation_price=None)
coop = models.Package(name="20g", quota_gb=20, duration_days=30, price=200_000, cooperation_price=120_000)
unlimited = models.Package(name="unl", quota_gb=0, duration_days=30, price=500_000)
db.add_all([pkg, coop, unlimited]); db.flush()
db.add(models.AdminPackagePrice(admin_id=seller.id, package_id=pkg.id, cooperation_price=95_000,
                                set_by_admin_id=root.id))
db.commit()

app = FastAPI()
app.include_router(packages_router.router)
app.dependency_overrides[get_db] = lambda: db
caller = {"admin": seller}
app.dependency_overrides[deps.get_current_admin] = lambda: db.get(models.AdminUser, caller["admin"].id)
client = TestClient(app)


def row(admin, package):
    caller["admin"] = admin
    response = client.get("/api/packages")
    assert response.status_code == 200, response.text
    found = next(p for p in response.json() if p["id"] == package.id)
    return found.get("my_cost", "absent"), found.get("my_cost_source", "absent")


check("own price is what the seller sees", lambda: row(seller, pkg), (95_000, "own_price"))
check("sale price is unchanged in the list", lambda: (caller.__setitem__("admin", seller),
      next(p["price"] for p in client.get("/api/packages").json() if p["id"] == pkg.id))[1], 300_000)
check("no own price -> package cooperation price", lambda: row(seller, coop), (120_000, "package_cooperation"))
check("no cooperation price -> package price", lambda: row(plain, pkg), (300_000, "package_price"))
check("usage billing pays nothing per package", lambda: row(usage, pkg), (0, "usage_billing"))
check("per-GB rate", lambda: row(per_gb, pkg), (50_000, "per_gb_rate"))
check("per-GB rate on an unlimited package is unknown, not an error", lambda: row(per_gb, unlimited),
      (None, "unpriceable"))
check("superadmin is never charged", lambda: row(root, pkg), (0, "superadmin"))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall package list cost checks passed")
