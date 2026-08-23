"""Bot sales charge the reseller - once the panel is switched to.

Run:  python3 backend/tests/test_bot_sale_charge.py

routers/bot.py never touched the billing service, so a purchase or renewal
through a reseller's own Telegram bot - the way most selling actually
happens - was free, while the identical action from the panel was charged.

The switch defaults OFF: turning it on is an operational decision, because
from that moment a reseller with no credit cannot complete a sale.
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
from app.routers import bot as bot_router
from app.routers import panel_settings as settings_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def setup(*, charging, balance=1_000_000, limit=0, owned=True):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.PanelSettings(id=1, charge_admins_for_bot_sales=charging))
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


print("--- the switch is off: nothing changes ---")
db, a, p, u = setup(charging=False)
check("a bot sale is free", (charge(db, u, p), a.balance), (True, 1_000_000))
check("so is a bot renewal", (charge(db, u, None, 20), a.balance), (True, 1_000_000))

print("\n--- the switch is on ---")
db, a, p, u = setup(charging=True)
charge(db, u, p)
db.refresh(a)
check("a bot sale costs the reseller", a.balance, 1_000_000 - 40_000)

db, a, p, u = setup(charging=True)
a.wholesale_price_per_gb = 1_000
db.commit()
charge(db, u, None, 20)
db.refresh(a)
check("a bot renewal in raw GB uses the per-GB rate", a.balance, 1_000_000 - 20_000)

print("\n--- an unaffordable sale ---")
db, a, p, u = setup(charging=True, balance=10_000)
check("is refused", charge(db, u, p), False)
db.refresh(a)
check("and takes nothing", a.balance, 10_000)

db, a, p, u = setup(charging=True, balance=10_000, limit=100_000)
check("the overdraft lets it through", charge(db, u, p), True)
db.refresh(a)
check("...into debt, which is what an overdraft is", a.balance, -30_000)

print("\n--- a customer with no reseller ---")
db, a, p, u = setup(charging=True, owned=False)
check("belongs to the superadmin, so nothing is charged", charge(db, u, p), True)
db.refresh(a)
check("the reseller's balance is untouched", a.balance, 1_000_000)

print("\n--- who may flip the switch ---")
db, a, p, u = setup(charging=False)
su = models.AdminUser(username="su", hashed_password="x", is_superadmin=True)
db.add(su)
db.commit()
db.refresh(su)

payload = schemas.PanelSettingsUpdate(charge_admins_for_bot_sales=True)
try:
    settings_router.update_settings(payload, db=db, admin=a)
    allowed = True
except HTTPException:
    db.rollback()
    allowed = False
check("a level-2 Admin cannot turn their own billing off", allowed, False)
check("...and the setting did not move",
      db.get(models.PanelSettings, 1).charge_admins_for_bot_sales, False)

try:
    settings_router.update_settings(payload, db=db, admin=su)
    allowed = True
except HTTPException:
    db.rollback()
    allowed = False
check("the superadmin can", allowed, True)
check("...and it took effect",
      db.get(models.PanelSettings, 1).charge_admins_for_bot_sales, True)

print("\n--- a panel with no settings row at all ---")
engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
bare = sessionmaker(bind=engine)()
a2 = models.AdminUser(username="a2", hashed_password="x", balance=5_000, role="admin")
p2 = models.Package(name="p", quota_gb=10, cooperation_price=99_000)
bare.add_all([a2, p2])
bare.commit()
bare.refresh(a2)
bare.refresh(p2)
u2 = models.User(username="c2", owner_admin_id=a2.id)
bare.add(u2)
bare.commit()
bare.refresh(u2)
# Fails safe: no row means no instruction to charge, so selling keeps working.
check("selling still works", charge(bare, u2, p2), True)
bare.refresh(a2)
check("and nothing was charged", a2.balance, 5_000)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
