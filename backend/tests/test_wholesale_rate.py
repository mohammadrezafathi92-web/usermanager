"""The per-GB wholesale rate: what an Admin owes upward is the superadmin's
number, not their own.

Run:  python3 backend/tests/test_wholesale_rate.py

Package.cooperation_price decides what a level-2 Admin pays, and a level-2
Admin builds and prices their own packages - so that field was a number
they set for themselves. The credit system metered nothing for exactly the
accounts it existed to meter.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import admin_billing

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def admin(**kw):
    return models.AdminUser(
        username=kw.get("username", "a1"), hashed_password="x", is_superadmin=kw.get("su", False),
        balance=kw.get("balance", 0), credit_limit=kw.get("limit", 0),
        wholesale_price_per_gb=kw.get("rate", 0), billing_mode=kw.get("billing", "flat"),
    )


def pkg(quota_gb, *, coop=None, price=0, name="p"):
    return models.Package(name=name, quota_gb=quota_gb, price=price, cooperation_price=coop)


def price(a, p):
    try:
        return admin_billing.unit_price(a, p)
    except HTTPException as exc:
        return f"REFUSED: {exc.detail}"


print("--- with no rate set, nothing changes ---")
a = admin(rate=0)
check("cooperation price wins when present", price(a, pkg(50, coop=30_000, price=90_000)), 30_000)
check("falls back to the customer price", price(a, pkg(50, price=90_000)), 90_000)
check("a free package is free", price(a, pkg(50)), 0)
check("an unlimited package is allowed", price(a, pkg(0, coop=25_000)), 25_000)

print("\n--- with a rate, the admin's own number is ignored ---")
a = admin(rate=1_000)
check("50GB at 1,000/GB", price(a, pkg(50, coop=1)), 50_000)
check("...even when they set cooperation_price to zero", price(a, pkg(50, coop=0)), 50_000)
check("...and when they set it very high", price(a, pkg(50, coop=9_999_999)), 50_000)
check("10GB", price(a, pkg(10, coop=0)), 10_000)
check("fractional quota rounds", price(a, pkg(1.5, coop=0)), 1_500)
check("a half-toman result rounds rather than truncating",
      admin_billing.unit_price(admin(rate=3), pkg(0.5)), 2)

print("\n--- an unlimited package has no per-GB answer ---")
out = price(admin(rate=1_000, username="reza"), pkg(0, coop=0, name="نامحدود"))
check("it is refused, not silently free", str(out).startswith("REFUSED"), True)
check("...the message names the admin", "reza" in str(out), True)
check("...and the package", "نامحدود" in str(out), True)
check("...and says how to resolve it", "نرخ گیگی" in str(out), True)

print("\n--- charging and refunding use the SAME number ---")
db = make_db()
a = admin(rate=2_000, balance=500_000)
p = pkg(25, coop=1)   # 25 x 2,000 = 50,000
db.add_all([a, p])
db.commit()
db.refresh(a)
db.refresh(p)

admin_billing.charge_for_package(db, a, p, units=3)
db.refresh(a)
check("3 units charged at the rate", a.balance, 500_000 - 150_000)

admin_billing.refund_for_package(db, a, p, units=3)
db.refresh(a)
check("refunding the same 3 restores exactly", a.balance, 500_000)

print("\n--- the rate respects everything already built ---")
db = make_db()
a = admin(rate=1_000, balance=10_000, limit=0)
p = pkg(50)  # 50,000 - more than they have
db.add_all([a, p])
db.commit()
db.refresh(a)
db.refresh(p)
try:
    admin_billing.charge_for_package(db, a, p)
    refused = False
except HTTPException:
    refused = True
db.refresh(a)
check("an unaffordable package is refused", refused, True)
check("nothing was deducted", a.balance, 10_000)

a.credit_limit = 100_000
db.commit()
admin_billing.charge_for_package(db, a, p)
db.refresh(a)
check("the overdraft still applies", a.balance, -40_000)

db = make_db()
a = admin(rate=1_000, balance=0, billing="usage")
p = pkg(50)
db.add_all([a, p])
db.commit()
db.refresh(a)
db.refresh(p)
admin_billing.charge_for_package(db, a, p)
db.refresh(a)
check("a volume-billed admin is still not charged per package", a.balance, 0)

db = make_db()
su = admin(su=True, rate=1_000, balance=0, username="su")
p = pkg(50)
db.add_all([su, p])
db.commit()
db.refresh(su)
db.refresh(p)
admin_billing.charge_for_package(db, su, p)
db.refresh(su)
check("a superadmin is never charged", su.balance, 0)

print("\n--- a row from before the column existed ---")
class Old:
    is_superadmin = False
    billing_mode = "flat"
    username = "old"


check("falls back to the package price",
      admin_billing.unit_price(Old(), pkg(50, coop=7_000)), 7_000)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
