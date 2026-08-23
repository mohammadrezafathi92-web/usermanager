"""Renewing costs the admin, the same as selling does.

Run:  python3 backend/tests/test_renewal_charge.py

Renewals were free from the credit system's point of view - only creating a
customer and adding a package were charged. On a panel whose customers
mostly renew, that is most of the revenue passing through unmetered.
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


def setup(**kw):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    a = models.AdminUser(
        username="a1", hashed_password="x", is_superadmin=kw.get("su", False),
        balance=kw.get("balance", 1_000_000), credit_limit=kw.get("limit", 0),
        wholesale_price_per_gb=kw.get("rate", 0), billing_mode=kw.get("billing", "flat"),
    )
    p = models.Package(name="۵۰ گیگ", quota_gb=50, price=90_000, cooperation_price=kw.get("coop", 40_000))
    db.add_all([a, p])
    db.commit()
    db.refresh(a)
    db.refresh(p)
    return db, a, p


def renew(db, a, package, add_gb):
    try:
        admin_billing.charge_for_renewal(db, a, package, add_gb)
        ok = True
    except HTTPException:
        db.rollback()
        ok = False
    db.refresh(a)
    return ok, a.balance


print("--- renewing WITH a package costs what selling it costs ---")
db, a, p = setup(balance=1_000_000)
check("charged the cooperation price", renew(db, a, p, 0), (True, 960_000))
check("a second renewal charges again", renew(db, a, p, 0), (True, 920_000))

db, a, p = setup(balance=1_000_000, rate=2_000)
check("with a per-GB rate, the rate wins over cooperation_price",
      renew(db, a, p, 0), (True, 900_000))  # 50GB x 2,000

print("\n--- renewing with RAW gigabytes ---")
db, a, p = setup(balance=1_000_000, rate=1_000)
check("10GB at 1,000/GB", renew(db, a, None, 10), (True, 990_000))
check("fractional gigabytes", renew(db, a, None, 2.5), (True, 987_500))

# Without a rate there is no price for a bare gigabyte, and inventing one
# would be guessing at the operator's pricing.
db, a, p = setup(balance=1_000_000, rate=0)
check("no rate set: raw gigabytes stay free", renew(db, a, None, 10), (True, 1_000_000))
check("zero gigabytes costs nothing", renew(db, a, None, 0), (True, 1_000_000))

print("\n--- an admin who cannot afford it is refused ---")
db, a, p = setup(balance=30_000)  # package costs 40,000
ok, balance = renew(db, a, p, 0)
check("refused", ok, False)
check("and nothing was taken", balance, 30_000)

db, a, p = setup(balance=30_000, limit=50_000)
check("the overdraft applies to renewals too", renew(db, a, p, 0), (True, -10_000))

print("\n--- who is never charged ---")
db, a, p = setup(su=True, balance=0)
check("a superadmin renews for free", renew(db, a, p, 0), (True, 0))
db, a, p = setup(billing="usage", balance=0)
check("a volume-billed admin is not charged per renewal", renew(db, a, p, 0), (True, 0))

print("\n--- the charge and the renewal are one decision ---")
# The endpoint charges BEFORE applying, so a refusal must leave the service
# untouched rather than renewing it into a debt.
db, a, p = setup(balance=0)
before = a.balance
ok, after = renew(db, a, p, 0)
check("a refused renewal changes no balance", (ok, after), (False, before))

print("\n--- bulk: one debit for the whole batch ---")
db, a, p = setup(balance=1_000_000)
admin_billing.charge_for_package(db, a, p, units=5)
db.refresh(a)
check("5 users x 40,000", a.balance, 1_000_000 - 200_000)

db, a, p = setup(balance=100_000)
try:
    admin_billing.charge_for_package(db, a, p, units=5)  # 200,000 needed
    ok = True
except HTTPException:
    db.rollback()
    ok = False
db.refresh(a)
check("a batch they cannot afford is refused whole", ok, False)
check("...taking nothing, not part of it", a.balance, 100_000)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
