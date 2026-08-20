"""The overdraft: how far below zero a reseller's balance may go.

Run:  python3 backend/tests/test_credit_limit.py

Money, so the cases that matter are the boundaries and the concurrent one -
the check lives in the UPDATE's WHERE clause precisely so two sales that
each look affordable cannot both succeed.
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
from app.routers import users as users_router

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


def setup(balance, limit, cost, *, billing="flat"):
    db = make_db()
    admin = models.AdminUser(
        username="reseller", hashed_password="x", is_superadmin=False,
        balance=balance, credit_limit=limit, billing_mode=billing,
    )
    pkg = models.Package(name="p", price=cost, cooperation_price=cost)
    db.add_all([admin, pkg])
    db.commit()
    db.refresh(admin)
    db.refresh(pkg)
    return db, admin, pkg


def charge(db, admin, pkg, units=1):
    """(ok, resulting balance)."""
    try:
        users_router._charge_admin_for_package(db, admin, pkg, units=units)
        ok = True
    except HTTPException:
        ok = False
    db.refresh(admin)
    return ok, admin.balance


print("--- no overdraft: the old hard floor ---")
db, a, p = setup(balance=10_000, limit=0, cost=10_000)
check("exactly affordable succeeds", charge(db, a, p), (True, 0))

db, a, p = setup(balance=9_999, limit=0, cost=10_000)
check("one toman short is refused", charge(db, a, p), (False, 9_999))
check("...and nothing was deducted", a.balance, 9_999)

print("\n--- with an overdraft ---")
db, a, p = setup(balance=0, limit=50_000, cost=10_000)
check("an empty balance can still sell", charge(db, a, p), (True, -10_000))

db, a, p = setup(balance=0, limit=10_000, cost=10_000)
check("spending exactly the limit is allowed", charge(db, a, p), (True, -10_000))

db, a, p = setup(balance=0, limit=9_999, cost=10_000)
check("one toman past the limit is refused", charge(db, a, p), (False, 0))

db, a, p = setup(balance=-40_000, limit=50_000, cost=10_000)
check("already in debt, still within the limit", charge(db, a, p), (True, -50_000))

db, a, p = setup(balance=-50_000, limit=50_000, cost=1)
check("at the limit exactly, one more toman is refused", charge(db, a, p), (False, -50_000))

print("\n--- the refusal message tells the truth ---")
db, a, p = setup(balance=5_000, limit=20_000, cost=100_000)
try:
    users_router._charge_admin_for_package(db, a, p)
    detail = ""
except HTTPException as exc:
    detail = str(exc.detail)
check("it names the overdraft", "20,000" in detail, True)
check("it names what is actually available", "25,000" in detail, True)

db, a, p = setup(balance=5_000, limit=0, cost=100_000)
try:
    users_router._charge_admin_for_package(db, a, p)
    detail = ""
except HTTPException as exc:
    detail = str(exc.detail)
check("with no overdraft it does not mention one", "سقف بدهی" in detail, False)

print("\n--- multiple units are charged as one decision ---")
db, a, p = setup(balance=0, limit=25_000, cost=10_000)
check("3 units over the limit are refused outright", charge(db, a, p, units=3), (False, 0))
check("2 units fit", charge(db, a, p, units=2), (True, -20_000))

print("\n--- who is never charged ---")
db = make_db()
su = models.AdminUser(username="su", hashed_password="x", is_superadmin=True, balance=0, credit_limit=0)
pkg = models.Package(name="p", price=99_999, cooperation_price=99_999)
db.add_all([su, pkg])
db.commit()
db.refresh(su)
db.refresh(pkg)
check("a superadmin is never charged", charge(db, su, pkg), (True, 0))

db, a, p = setup(balance=0, limit=0, cost=10_000, billing="usage")
check("a volume-billed admin is not charged per package", charge(db, a, p), (True, 0))

print("\n--- a missing column must not break an old row ---")
db, a, p = setup(balance=10_000, limit=0, cost=5_000)


class NoColumn:
    """An admin object from before credit_limit existed."""
    id = a.id
    is_superadmin = False
    billing_mode = "flat"
    balance = 10_000


try:
    # Not via charge(): db.refresh() only works on mapped instances, and
    # the point here is precisely that this object is not one.
    users_router._charge_admin_for_package(db, NoColumn(), p)
    outcome = True
except HTTPException:
    outcome = False
db.refresh(a)
check("getattr fallback keeps the old floor", (outcome, a.balance), (True, 5_000))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
