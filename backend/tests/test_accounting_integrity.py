"""The accounting audit of 2026-09, locked in.

Run:  python3 backend/tests/test_accounting_integrity.py

Each block below is one of the reported problems ("توی تب حسابداری یه سری
چیزا اشتباه، منطق‌های حسابداری توش رعایت نمیشه"):

  #2  the superadmin's "sales" counted every reseller's retail sale as
      their own income, while the money resellers actually paid them
      counted as nothing at all
  #3  deleting a reseller re-assigned that reseller's entire history to
      the superadmin, because admin_id is SET NULL and NULL means "the
      superadmin's own business"
  #6  the card-cash figure silently dropped every row whose payment_method
      was never recorded (NULL != 'wallet' is NULL in SQL, not true)
  #7  a negative margin was reported as "unknown" rather than as a loss
  #9  the chart's default window was measured from today even when the
      caller asked for a period that ended earlier
  #10 credit handed to a reseller before they paid for it was treated as
      money received
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
NOW = dt.datetime.utcnow()


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


def add_admin(db, username, *, superadmin=False, parent=None, role=None,
              balance=0, billing_mode="flat"):
    row = models.AdminUser(
        username=username, hashed_password="x", is_superadmin=superadmin,
        parent_admin_id=parent.id if parent else None, role=role,
        balance=balance, billing_mode=billing_mode,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def entry(db, kind, amount, *, admin=None, when=None, payment_method=None):
    accounting.record(
        db, kind, amount,
        admin_id=admin.id if admin else None,
        payment_method=payment_method,
        created_at=when or NOW,
    )
    db.commit()


# ---------------------------------------------------------------- #2, #10
print("--- #2/#10 the superadmin's profit is their OWN business, not the tree's ---")
db = make_db()
su = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
seller = add_admin(db, "r1", parent=su, role=hierarchy.ROLE_ADMIN)

entry(db, "sale_new", 300_000)                     # the superadmin's own customer
entry(db, "sale_new", 1_000_000, admin=seller)     # the reseller's retail sale
entry(db, "admin_credit_change", 800_000, admin=seller)  # credit granted, unpaid
entry(db, "expense", 100_000)

s = accounting.summary(db, su)
check("own sales are reported on their own", s["own_sales_total"], 300_000)
check("the reseller's retail is shown separately as turnover",
      s["reseller_sales_total"], 1_000_000)
check("granting credit is NOT income", s["admin_payments_total"], 0)
check("profit excludes the reseller's retail and the unpaid credit",
      s["net_profit"], 300_000 - 100_000)
check("what the reseller owes is visible", s["receivables_total"], 800_000)

print("\n--- ...and once the money is actually collected ---")
entry(db, "admin_payment", 500_000, admin=seller)
s = accounting.summary(db, su)
check("the collected part becomes income", s["admin_payments_total"], 500_000)
check("profit picks it up", s["net_profit"], 300_000 + 500_000 - 100_000)
check("and it comes off what is still owed", s["receivables_total"], 300_000)

rows = accounting.receivables_by_admin(db, su)
check("the reseller appears in the receivables list", len(rows), 1)
check("...charged", rows[0]["charged_total"], 800_000)
check("...paid", rows[0]["paid_total"], 500_000)
check("...outstanding", rows[0]["owed"], 300_000)

print("\n--- a deleted reseller drops off the collections list ---")
db_del = make_db()
su_d = add_admin(db_del, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
alive = add_admin(db_del, "alive", parent=su_d, role=hierarchy.ROLE_ADMIN)
dead = add_admin(db_del, "dead", parent=su_d, role=hierarchy.ROLE_ADMIN)
entry(db_del, "admin_credit_change", 700_000, admin=alive)
entry(db_del, "admin_credit_change", 500_000, admin=dead)
db_del.query(models.LedgerEntry).filter(models.LedgerEntry.admin_id == dead.id).update(
    {"admin_id": None}, synchronize_session=False
)
db_del.delete(dead)
db_del.commit()
rows_d = accounting.receivables_by_admin(db_del, su_d)
check("only the live account is listed", [r["username"] for r in rows_d], ["alive"])
check("and the total matches the rows shown",
      accounting.receivables_total(db_del, su_d), 700_000)

print("\n--- resetting starts the account from now ---")
settings = models.PanelSettings()
db_del.add(settings)
db_del.commit()
settings.receivables_start_at = dt.datetime.utcnow()
db_del.commit()
check("everything owed before the line stops counting",
      accounting.receivables_total(db_del, su_d), 0)
entry(db_del, "admin_credit_change", 90_000, admin=alive,
      when=dt.datetime.utcnow() + dt.timedelta(seconds=1))
check("...and new credit after it counts normally",
      accounting.receivables_for_admin(db_del, alive.id), 90_000)

print("\n--- usage charges are debts too (حجمی) ---")
db2 = make_db()
su2 = add_admin(db2, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
u_admin = add_admin(db2, "vol", parent=su2, role=hierarchy.ROLE_ADMIN, billing_mode="usage")
entry(db2, "admin_usage_charge", 250_000, admin=u_admin)
check("metered traffic puts a usage-billed reseller in debt",
      accounting.receivables_for_admin(db2, u_admin.id), 250_000)
check("...and a usage charge is a cost in that reseller's own books",
      accounting.summary(db2, u_admin)["credit_spent_total"], 250_000)
check("...whose balance is reported in GB, not tomans",
      accounting.summary(db2, u_admin)["credit_balance"], None)


# -------------------------------------------------------------------- #3
print("\n--- #3 a deleted reseller's history stays theirs ---")
db3 = make_db()
su3 = add_admin(db3, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
gone = add_admin(db3, "leaver", parent=su3, role=hierarchy.ROLE_ADMIN)
entry(db3, "sale_new", 400_000, admin=gone)
entry(db3, "sale_new", 100_000)  # the superadmin's own

before = accounting.summary(db3, su3)["own_sales_total"]
# Exactly what deleting the admin does to the ledger: the FK is cleared.
db3.query(models.LedgerEntry).filter(models.LedgerEntry.admin_id == gone.id).update(
    {"admin_id": None}, synchronize_session=False
)
db3.delete(gone)
db3.commit()
after = accounting.summary(db3, su3)["own_sales_total"]
check("the superadmin's own sales do not absorb the deleted reseller's",
      (before, after), (100_000, 100_000))


# -------------------------------------------------------------------- #6
print("\n--- #6 card cash counts rows with no payment_method recorded ---")
db4 = make_db()
su4 = add_admin(db4, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
entry(db4, "sale_new", 100_000, payment_method="card")
entry(db4, "sale_new", 50_000, payment_method=None)     # older/backfilled row
entry(db4, "sale_new", 70_000, payment_method="wallet")  # already-received money
check("cash = card + unknown, and never the wallet-paid one",
      accounting.summary(db4, su4)["card_cash_total"], 150_000)


# -------------------------------------------------------------------- #7
print("\n--- #7 a loss is shown as a loss, not as a blank ---")
db5 = make_db()
su5 = add_admin(db5, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
r5 = add_admin(db5, "r5", parent=su5, role=hierarchy.ROLE_ADMIN)
entry(db5, "admin_credit_spend", 90_000, admin=r5)
check("spent credit with no sales yet is a negative margin",
      accounting.summary(db5, r5)["margin_total"], -90_000)
db6 = make_db()
su6 = add_admin(db6, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
r6 = add_admin(db6, "r6", parent=su6, role=hierarchy.ROLE_ADMIN)
check("a genuinely empty period still has no answer",
      accounting.summary(db6, r6)["margin_total"], None)


# -------------------------------------------------------------------- #9
print("\n--- #9 the chart window follows the period that was asked for ---")
db7 = make_db()
su7 = add_admin(db7, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
long_ago = NOW - dt.timedelta(days=120)
entry(db7, "sale_new", 42_000, when=long_ago)
series = accounting.series(db7, su7, "day", date_to=long_ago + dt.timedelta(days=1))
check("asking only for an old end-date still returns that period's data",
      sum(b["sales"] for b in series), 42_000)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("دفترها حالا با منطق حسابداری می‌خوانند")
