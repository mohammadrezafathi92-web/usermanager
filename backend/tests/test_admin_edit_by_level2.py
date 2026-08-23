"""A level-2 Admin must be able to edit their own Seller - without that
edit becoming a way to mint credit.

Run:  python3 backend/tests/test_admin_edit_by_level2.py

The balance guard was written as "was this field sent?" instead of "did
this field change?". The edit form displays the balance read-only and
posts it back with every save, so a level-2 Admin got a 403 about credit
transfer no matter what they were actually editing - a permission tick, a
Telegram id, a password. Reported as "I ticked accounting and it will not
save".

The hole the guard exists to close is real, so these tests check BOTH
directions: an unchanged balance saves, a changed one is still refused.
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
from app.routers import admins as admins_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    su = models.AdminUser(username="su", hashed_password="x", is_superadmin=True)
    db.add(su)
    db.commit()
    db.refresh(su)

    lvl2 = models.AdminUser(username="test", hashed_password="x", role="admin", parent_admin_id=su.id)
    db.add(lvl2)
    db.commit()
    db.refresh(lvl2)

    seller = models.AdminUser(
        username="testt", hashed_password="x", role="seller",
        parent_admin_id=lvl2.id, balance=1_500_000, permissions="",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)
    return db, su, lvl2, seller


def edit(db, actor, target, **fields):
    """Returns (ok, detail) - never raises, so a refusal is inspectable."""
    payload = schemas.AdminUpdate(**fields)
    try:
        admins_router.update_admin(target.id, payload, db=db, current=actor)
        db.commit()
        return True, None
    except HTTPException as exc:
        db.rollback()
        return False, exc.detail


print("--- what the edit form actually posts ---")
# Exactly the payload Admins.jsx sends for a level-2 Admin saving a
# permission tick: the read-only balance comes along unchanged.
db, su, lvl2, seller = build()
ok, detail = edit(db, lvl2, seller, permissions=["view_accounting"], balance=1_500_000)
check("saving a permission tick succeeds", ok, True)
if not ok:
    print(f"        refused with: {detail}")
db.refresh(seller)
check("...the permission stuck", "view_accounting" in (seller.permissions or ""), True)
check("...and the balance is untouched", seller.balance, 1_500_000)

print("\n--- the hole this guard exists to close is still shut ---")
db, su, lvl2, seller = build()
ok, detail = edit(db, lvl2, seller, balance=9_000_000)
check("a level-2 Admin cannot raise their Seller's balance", ok, False)
check("...and is told to use credit transfer", "انتقال اعتبار" in str(detail), True)
db.refresh(seller)
check("...the balance really did not move", seller.balance, 1_500_000)

ok, _ = edit(db, lvl2, seller, balance=100_000)
check("nor lower it", ok, False)

print("\n--- a superadmin still may ---")
db, su, lvl2, seller = build()
ok, detail = edit(db, su, seller, balance=2_000_000)
check("superadmin sets an absolute balance", ok, True)
db.refresh(seller)
check("...it applied", seller.balance, 2_000_000)
logs = db.query(models.AdminBalanceLog).filter(models.AdminBalanceLog.admin_id == seller.id).all()
check("...and it was logged, not silent", len(logs), 1)
check("...with the right amount", logs[0].amount if logs else None, 500_000)
check("...and the resulting balance", logs[0].balance_after if logs else None, 2_000_000)

print("\n--- other fields a level-2 Admin edits on their Seller ---")
db, su, lvl2, seller = build()
for label, fields in [
    ("telegram id", {"telegram_id": 12345, "balance": 1_500_000}),
    ("login slug", {"login_slug": "myseller", "balance": 1_500_000}),
    ("billing mode", {"billing_mode": "usage", "balance": 1_500_000}),
]:
    ok, detail = edit(db, lvl2, seller, **fields)
    check(f"saving {label}", ok, True)
    if not ok:
        print(f"        refused with: {detail}")

print("\n--- and the overdraft stays superadmin-only ---")
db, su, lvl2, seller = build()
ok, detail = edit(db, lvl2, seller, credit_limit=500_000)
check("a level-2 Admin cannot grant an overdraft", ok, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
