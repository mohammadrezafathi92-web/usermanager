"""Credit is delegated down the tree, never invented.

Run:  python3 backend/tests/test_credit_delegation.py

The rule: the superadmin is the only source. Everyone else can pass on what
they hold and no more, and the giver's balance falls by exactly what the
receiver's rises - so the total credit in the tree only changes when the
superadmin changes it.
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
from app.services import hierarchy
from app.routers import admins as admins_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


admins_router._out = lambda db, admin: admin  # type: ignore[assignment]


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add(db, username, *, superadmin=False, parent=None, role=None, balance=0, limit=0, volume=0):
    row = models.AdminUser(
        username=username, hashed_password="x", is_superadmin=superadmin,
        parent_admin_id=parent.id if parent else None, role=role,
        balance=balance, credit_limit=limit, volume_balance_gb=volume,
        billing_mode="usage" if volume else "flat",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def topup(db, target, actor, amount):
    payload = schemas.AdminTopupRequest(amount=amount, note=None)
    try:
        admins_router.topup_admin_balance(target.id, payload, db=db, current=actor)
        ok = True
    except HTTPException:
        db.rollback()
        ok = False
    db.refresh(target)
    db.refresh(actor)
    return ok


print("--- the superadmin creates credit ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN, balance=0)

check("superadmin tops up an Admin", topup(db, boss, sa, 100_000), True)
check("...and the Admin has it", boss.balance, 100_000)
check("...and the superadmin's own balance is untouched", sa.balance, 0)

print("\n--- an Admin passes credit to their own Seller ---")
seller = add(db, "seller", parent=boss, role=hierarchy.ROLE_SELLER, balance=0)

check("giving 40k succeeds", topup(db, seller, boss, 40_000), True)
check("the Seller received it", seller.balance, 40_000)
check("the Admin paid for it", boss.balance, 60_000)
check("total in the tree is unchanged", boss.balance + seller.balance, 100_000)

print("\n--- and cannot give what they do not have ---")
check("giving more than they hold is refused", topup(db, seller, boss, 60_001), False)
check("the Seller's balance is untouched", seller.balance, 40_000)
check("the Admin's balance is untouched", boss.balance, 60_000)
check("giving exactly everything is allowed", topup(db, seller, boss, 60_000), True)
check("the Admin is now empty", boss.balance, 0)

print("\n--- an overdraft is not spendable credit ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
# 50k of real credit, plus permission to sell 500k past zero.
boss = add(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN, balance=50_000, limit=500_000)
seller = add(db, "seller", parent=boss, role=hierarchy.ROLE_SELLER)
check("the overdraft cannot be handed down", topup(db, seller, boss, 200_000), False)
check("only the real balance can", topup(db, seller, boss, 50_000), True)
check("the Admin is at zero, not negative", boss.balance, 0)

print("\n--- taking credit back ---")
check("the Admin can reclaim it", topup(db, seller, boss, -30_000), True)
check("the Seller lost it", seller.balance, 20_000)
check("the Admin got it back", boss.balance, 30_000)
# A Seller who already spent it may go negative, but only as far as their
# own ceiling allows.
seller.credit_limit = 5_000
db.commit()
check("a takeback past the Seller's debt ceiling is refused",
      topup(db, seller, boss, -26_000), False)
check("...but one that lands exactly on it is allowed",
      topup(db, seller, boss, -25_000), True)
check("the Seller is at their ceiling", seller.balance, -5_000)

print("\n--- who may give to whom ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add(db, "a1", parent=sa, role=hierarchy.ROLE_ADMIN, balance=100_000)
a2 = add(db, "a2", parent=sa, role=hierarchy.ROLE_ADMIN, balance=0)
foreign = add(db, "foreign", parent=a2, role=hierarchy.ROLE_SELLER)

check("an Admin cannot fund another Admin", topup(db, a2, a1, 10_000), False)
check("an Admin cannot fund someone else's Seller", topup(db, foreign, a1, 10_000), False)
check("a1's balance survived both attempts", a1.balance, 100_000)

print("\n--- the absolute-set back door is closed ---")
own = add(db, "own", parent=a1, role=hierarchy.ROLE_SELLER, balance=0)
payload = schemas.AdminUpdate(balance=999_999)
try:
    admins_router.update_admin(own.id, payload, db=db, current=a1)
    allowed = True
except HTTPException:
    db.rollback()
    allowed = False
db.refresh(own)
check("an Admin cannot set a Seller's balance directly", allowed, False)
check("...and nothing was minted", own.balance, 0)

# The superadmin may still use it.
try:
    admins_router.update_admin(own.id, schemas.AdminUpdate(balance=7_000), db=db, current=sa)
    allowed = True
except HTTPException:
    db.rollback()
    allowed = False
db.refresh(own)
check("the superadmin still can", (allowed, own.balance), (True, 7_000))

print("\n--- volume is delegated by the same rule ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN, volume=100)
seller = add(db, "seller", parent=boss, role=hierarchy.ROLE_SELLER, volume=0)


def vol_topup(db, target, actor, gb):
    payload = schemas.AdminVolumeTopupRequest(amount_gb=gb, note=None)
    try:
        admins_router.topup_admin_volume(target.id, payload, db=db, current=actor)
        ok = True
    except HTTPException:
        db.rollback()
        ok = False
    db.refresh(target)
    db.refresh(actor)
    return ok


check("giving 30GB works", vol_topup(db, seller, boss, 30), True)
check("the Seller has it", seller.volume_balance_gb, 30)
check("the Admin paid for it", boss.volume_balance_gb, 70)
check("giving more than they hold is refused", vol_topup(db, seller, boss, 71), False)
check("the superadmin can still create volume", vol_topup(db, boss, sa, 500), True)
check("...without losing any themselves", sa.volume_balance_gb, 0)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
