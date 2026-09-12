"""The aggregate view of a subtree: one row per direct sub-account.

Run:  python3 backend/tests/test_subtree_rollup.py
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
from app.services import accounting, hierarchy

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


def add(db, username, *, superadmin=False, parent=None, role=None, balance=0, limit=0):
    row = models.AdminUser(
        username=username, hashed_password="x", is_superadmin=superadmin,
        parent_admin_id=parent.id if parent else None, role=role,
        balance=balance, credit_limit=limit,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def add_user(db, username, owner, status=models.UserStatus.active):
    db.add(models.User(username=username, owner_admin_id=owner.id, status=status))
    db.commit()


def add_sale(db, admin, amount, *, when=None, kind="sale_new"):
    db.add(models.LedgerEntry(
        kind=kind, amount=amount, admin_id=admin.id,
        created_at=when or dt.datetime.utcnow(),
    ))
    db.commit()


def by_name(rows):
    return {r["username"]: r for r in rows}


db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN, balance=100_000)
s1 = add(db, "s1", parent=boss, role=hierarchy.ROLE_SELLER, balance=25_000)
s2 = add(db, "s2", parent=boss, role=hierarchy.ROLE_SELLER, balance=-5_000, limit=10_000)
# A root Admin, like the four this panel really has.
root = add(db, "root", role=hierarchy.ROLE_ADMIN, balance=7_000)

add_user(db, "c1", s1)
add_user(db, "c2", s1)
add_user(db, "c3", s1, status=models.UserStatus.disabled)
add_user(db, "c4", s2)
add_user(db, "c5", boss)

add_sale(db, s1, 50_000)
add_sale(db, s1, 30_000, kind="sale_renew")
add_sale(db, s2, 20_000)
add_sale(db, boss, 90_000)          # the Admin's own sale, not a Seller's
add_sale(db, s1, 999, kind="expense")  # not a sale

print("--- an Admin sees their own Sellers ---")
rows = accounting.subtree_rollup(db, boss)
check("one row per Seller", sorted(r["username"] for r in rows), ["s1", "s2"])
check("the Admin's own sale is not in the list",
      "boss" in by_name(rows), False)

r = by_name(rows)["s1"]
check("customers counted", r["customers"], 3)
check("active counted separately", r["active_customers"], 2)
check("sales summed across kinds", r["sales_total"], 80_000)
check("sale count", r["sales_count"], 2)
check("an expense is not a sale", r["sales_total"], 80_000)
check("balance shown", r["balance"], 25_000)
check("not flagged as in debt", r["in_debt"], False)

r = by_name(rows)["s2"]
check("a Seller with no sales still appears", r["customers"], 1)
check("debt is flagged", r["in_debt"], True)
check("their overdraft is shown", r["credit_limit"], 10_000)

print("\n--- a Seller has no sub-accounts ---")
check("empty rather than an error", accounting.subtree_rollup(db, s1), [])

print("\n--- the superadmin sees Admins, including the roots ---")
rows = accounting.subtree_rollup(db, sa)
check("both the child Admin and the root Admin appear",
      sorted(r["username"] for r in rows), ["boss", "root"])
check("the superadmin does not list itself", "super" in by_name(rows), False)
r = by_name(rows)["boss"]
# CHANGED 2026-09 ("تب زیرمجموعه‌های من درست کار نمی‌کنه"): these two used
# to assert that an Admin's row counted only that Admin's OWN customers
# and sales - so a level-2 Admin whose business runs through their Sellers
# showed 1 customer and 90,000 here while their branch really carried 5
# customers and 190,000. Nothing was missing from the database; the rollup
# just wasn't rolling anything up. A row now covers the whole branch, with
# the Admin's own share still available beside it.
check("an Admin's row rolls up their Sellers' customers too", r["customers"], 5)
check("...and their Sellers' sales", r["sales_total"], 190_000)
check("the Admin's own share is still visible separately", r["own_customers"], 1)
check("...for sales as well", r["own_sales_total"], 90_000)
check("and how many accounts sit under them", r["sub_accounts"], 2)
check("a root Admin with nothing under it rolls up to itself only",
      by_name(rows)["root"]["sub_accounts"], 0)

print("\n--- the date range is honoured ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN)
s1 = add(db, "s1", parent=boss, role=hierarchy.ROLE_SELLER)
old = dt.datetime.utcnow() - dt.timedelta(days=60)
add_sale(db, s1, 10_000, when=old)
add_sale(db, s1, 5_000)

check("no filter includes everything",
      by_name(accounting.subtree_rollup(db, boss))["s1"]["sales_total"], 15_000)
recent = accounting.subtree_rollup(db, boss, date_from=dt.datetime.utcnow() - dt.timedelta(days=7))
check("a from-date excludes the old sale", by_name(recent)["s1"]["sales_total"], 5_000)
check("...but the customer count is not date-filtered",
      by_name(recent)["s1"]["customers"], 0)

print("\n--- an account with nothing under it ---")
lonely = add(db, "lonely", parent=sa, role=hierarchy.ROLE_ADMIN)
check("returns an empty list", accounting.subtree_rollup(db, lonely), [])

print("\n--- a volume-billed Seller is judged by GB, not a stale toman balance ---")
# Switching an account to حجمی leaves its old toman balance frozen on the
# row. Reporting that number said an account with no credit at all still
# held millions, and "in debt" was decided from a pool that no longer
# governs anything (reported 2026-09). Its own database: adding a Seller
# to the shared tree above would change every count asserted there.
dbv = make_db()
sav = add(dbv, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
bossv = add(dbv, "boss", parent=sav, role=hierarchy.ROLE_ADMIN)
vol = add(dbv, "vol", parent=bossv, role=hierarchy.ROLE_SELLER, balance=9_250_000)
vol.billing_mode = "usage"
vol.volume_balance_gb = -12.5   # over-consumed
dbv.commit()
r = by_name(accounting.subtree_rollup(dbv, bossv))["vol"]
check("the row says it is volume-billed", r["billing_mode"], "usage")
check("its GB pool is reported", r["volume_balance_gb"], -12.5)
check("over-consumed GB counts as being in debt", r["in_debt"], True)
check("...and the leftover toman balance does not make it look healthy",
      (r["balance"], r["in_debt"]), (9_250_000, True))

flat = add(dbv, "flatone", parent=bossv, role=hierarchy.ROLE_SELLER, balance=-5_000)
dbv.commit()
rf = by_name(accounting.subtree_rollup(dbv, bossv))["flatone"]
check("a flat account is still judged by its toman balance", rf["billing_mode"], "flat")
check("...and a negative one is still flagged", rf["in_debt"], True)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
