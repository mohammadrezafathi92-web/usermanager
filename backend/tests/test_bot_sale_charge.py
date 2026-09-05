"""Bot sales always charge the reseller.

Run:  python3 backend/tests/test_bot_sale_charge.py

routers/bot.py never used to touch the billing service, so a purchase or
renewal through a reseller's own Telegram bot - the way most selling
actually happens - was free, while the identical action from the panel was
charged. That was first fixed behind a superadmin-only opt-in switch
(PanelSettings.charge_admins_for_bot_sales, default off); as of 2026-09-05
the switch itself was removed by explicit request - billing bot sales is no
longer optional, so _charge_seller now always charges, with no setting to
turn it off.
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
from app.routers import bot as bot_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def setup(*, balance=1_000_000, limit=0, owned=True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    a = models.AdminUser(username="reseller", hashed_password="x", balance=balance,
                         credit_limit=limit, role="admin")
    p = models.Package(name="۵۰ گیگ", quota_gb=50, price=90_000, cooperation_price=40_000)
    db.add_all([a, p])
    db.commit()
    db.refresh(a)
    db.refresh(p)
    u = models.User(username="c1", owner_admin_id=a.id if owned else None)
    db.add(u)
    db.commit()
    db.refresh(u)
    return db, a, p, u


def charge(db, u, p, add_gb=0):
    try:
        bot_router._charge_seller(db, u, p, add_gb)
        return True
    except HTTPException:
        db.rollback()
        return False


print("--- a bot sale always costs the reseller - no switch to turn it off ---")
db, a, p, u = setup()
charge(db, u, p)
db.refresh(a)
check("a bot sale costs the reseller", a.balance, 1_000_000 - 40_000)

db, a, p, u = setup()
a.wholesale_price_per_gb = 1_000
db.commit()
charge(db, u, None, 20)
db.refresh(a)
check("a bot renewal in raw GB uses the per-GB rate", a.balance, 1_000_000 - 20_000)

print("\n--- an unaffordable sale ---")
db, a, p, u = setup(balance=10_000)
check("is refused", charge(db, u, p), False)
db.refresh(a)
check("and takes nothing", a.balance, 10_000)

db, a, p, u = setup(balance=10_000, limit=100_000)
check("the overdraft lets it through", charge(db, u, p), True)
db.refresh(a)
check("...into debt, which is what an overdraft is", a.balance, -30_000)

print("\n--- a customer with no reseller ---")
db, a, p, u = setup(owned=False)
check("belongs to the superadmin, so nothing is charged", charge(db, u, p), True)
db.refresh(a)
check("the reseller's balance is untouched", a.balance, 1_000_000)

print("\n--- there is no PanelSettings row at all ---")
# The old code paths used to read PanelSettings.charge_admins_for_bot_sales
# here and could fail differently with no row present. The new code never
# touches PanelSettings, so a completely bare panel behaves identically.
engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
bare = sessionmaker(bind=engine)()
a2 = models.AdminUser(username="a2", hashed_password="x", balance=5_000, role="admin")
p2 = models.Package(name="p", quota_gb=10, cooperation_price=4_000)
bare.add_all([a2, p2])
bare.commit()
bare.refresh(a2)
bare.refresh(p2)
u2 = models.User(username="c2", owner_admin_id=a2.id)
bare.add(u2)
bare.commit()
bare.refresh(u2)
check("selling still works", charge(bare, u2, p2), True)
bare.refresh(a2)
check("and IS charged - no PanelSettings row needed anymore", a2.balance, 5_000 - 4_000)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
