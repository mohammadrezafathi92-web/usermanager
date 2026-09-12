"""Panel sales, local day boundaries, and usage billing.

Run:  python3 backend/tests/test_accounting_panel_sales.py

  #1  sale_new/sale_renew were recorded only by routers/bot.py, so a sale
      made from the WEB PANEL booked its cost and no revenue at all - the
      reseller's own page then showed rising costs against zero sales
  #4  the date picked in the panel is a LOCAL (Jalali) day but was compared
      against created_at, which is stored in UTC - every boundary sat 3.5
      hours out in Tehran
  #5  a usage-billed reseller's traffic was metered but never priced, so
      their cost of goods was always zero
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import accounting, hierarchy, jalali, usage_billing

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


# ------------------------------------------------------------------- #1
print("--- #1 a sale made from the panel books revenue, not just its cost ---")
db = make_db()
su = models.AdminUser(username="super", hashed_password="x", is_superadmin=True,
                       role=hierarchy.ROLE_SUPERADMIN)
db.add(su)
db.commit()
seller = models.AdminUser(username="r1", hashed_password="x", parent_admin_id=su.id,
                           role=hierarchy.ROLE_ADMIN, balance=0)
db.add(seller)
db.commit()
db.refresh(seller)

pkg = models.Package(name="p1", quota_gb=50, price=250_000, cooperation_price=150_000)
db.add(pkg)
db.commit()
db.refresh(pkg)

user = models.User(username="c1", owner_admin_id=seller.id)
db.add(user)
db.commit()
db.refresh(user)

accounting.record_panel_sale(db, "sale_new", user, pkg, actor_admin_id=seller.id)
db.commit()
s = accounting.summary(db, seller)
check("the sale is booked at the package price", s["sales_total"], 250_000)
check("...against the reseller who made it",
      db.query(models.LedgerEntry).first().owner_admin_id_snapshot, seller.id)

print("\n--- ...at the seller's OWN resale price when they set one ---")
db.add(models.PackageSellerPrice(package_id=pkg.id, seller_admin_id=seller.id, price=300_000))
db.commit()
accounting.record_panel_sale(db, "sale_renew", user, pkg, actor_admin_id=seller.id)
db.commit()
check("the renewal uses the seller's price", accounting.summary(db, seller)["sales_total"],
      250_000 + 300_000)

print("\n--- ...and a package-less user is not a sale ---")
before = db.query(models.LedgerEntry).count()
accounting.record_panel_sale(db, "sale_new", user, None, actor_admin_id=seller.id)
db.commit()
check("nothing was recorded", db.query(models.LedgerEntry).count(), before)


# ------------------------------------------------------------------- #4
print("\n--- #4 a picked date means a LOCAL day, not a UTC one ---")
from app.routers.accounting import _parse_date  # noqa: E402

offset = jalali.get_display_offset()
start = _parse_date("2026-09-10")
end = _parse_date("2026-09-10", end=True)
check("the range starts at local midnight expressed in UTC",
      start, dt.datetime(2026, 9, 10) - dt.timedelta(minutes=offset))
check("...and ends exactly 24h later",
      end - start, dt.timedelta(days=1))

# A sale at 01:00 Tehran on the 10th is 21:30 UTC on the 9th. It belongs
# to the 10th, which is what the customer and the operator both mean.
db2 = make_db()
su2 = models.AdminUser(username="super", hashed_password="x", is_superadmin=True,
                        role=hierarchy.ROLE_SUPERADMIN)
db2.add(su2)
db2.commit()
local_1am = dt.datetime(2026, 9, 10, 1, 0) - dt.timedelta(minutes=offset)
accounting.record(db2, "sale_new", 60_000, created_at=local_1am)
db2.commit()
got = accounting.summary(db2, su2, date_from=start, date_to=end)["own_sales_total"]
check("an early-morning local sale lands inside that local day", got, 60_000)


# ------------------------------------------------------------------- #5
print("\n--- #5 metered traffic becomes a priced charge ---")
db3 = make_db()
su3 = models.AdminUser(username="super", hashed_password="x", is_superadmin=True,
                        role=hierarchy.ROLE_SUPERADMIN)
db3.add(su3)
db3.commit()
vol = models.AdminUser(
    username="vol", hashed_password="x", parent_admin_id=su3.id, role=hierarchy.ROLE_ADMIN,
    billing_mode="usage", wholesale_price_per_gb=2_000, unbilled_usage_gb=12.5,
)
db3.add(vol)
db3.commit()
db3.refresh(vol)

charged = usage_billing.settle_usage_charges(db3)
db3.refresh(vol)
check("one admin was charged", charged, 1)
check("12.5 GB x 2,000 = 25,000",
      db3.query(models.LedgerEntry).filter(
          models.LedgerEntry.kind == "admin_usage_charge").first().amount, 25_000)
check("the meter is drained", round(vol.unbilled_usage_gb, 6), 0.0)
check("and it shows up as what they owe",
      accounting.receivables_for_admin(db3, vol.id), 25_000)

print("\n--- ...a second run with nothing new charges nothing ---")
check("no double billing", usage_billing.settle_usage_charges(db3), 0)

print("\n--- ...and an admin with no rate set is metered but not priced ---")
norate = models.AdminUser(
    username="norate", hashed_password="x", parent_admin_id=su3.id, role=hierarchy.ROLE_ADMIN,
    billing_mode="usage", wholesale_price_per_gb=0, unbilled_usage_gb=9.0,
)
db3.add(norate)
db3.commit()
db3.refresh(norate)
usage_billing.settle_usage_charges(db3)
db3.refresh(norate)
check("the meter still drains", round(norate.unbilled_usage_gb, 6), 0.0)
check("but no money row was invented", accounting.receivables_for_admin(db3, norate.id), 0)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("فروش پنل، روزِ محلی و صورتحساب حجمی همگی درست ثبت می‌شوند")
