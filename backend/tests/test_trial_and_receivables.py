"""Two rules that only matter when someone tries them.

Run:  python3 backend/tests/test_trial_and_receivables.py

1. A reseller must not appear in their own «طلب از نماینده‌ها» list.
   Reported 2026-09-14 with a screenshot: a level-2 admin looking at the
   receivables tab found THEMSELVES in it, owing 65,360, beside a «ثبت
   دریافت» button. The write endpoint already refused, so no money could
   actually move - but a button that appears to settle your own debt is not
   something to leave on screen and defend with a 400.

2. The free trial is free to the reseller, and hard to farm. Free plus
   Telegram is a farm unless all three limits hold at once: once per
   Telegram id, only for someone who has never bought, and a daily ceiling
   for the whole shop.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import accounting, trial

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refusal(fn, *a, **kw):
    try:
        fn(*a, **kw)
        return None
    except HTTPException as exc:
        return exc.detail


def fresh():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


# --------------------------------------------------------------------------
print("--- nobody is their own debtor ---")
db = fresh()
root = models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True)
boss = models.AdminUser(id=2, username="delradi", hashed_password="x")
seller = models.AdminUser(id=3, username="sub", hashed_password="x", parent_admin_id=2)
db.add_all([root, boss, seller])
db.add(models.PanelSettings(id=1))
db.commit()

# Both of them have been charged for credit they were granted.
accounting.record(db, accounting.RECEIVABLE_DEBIT_KINDS[0], 65360, admin_id=boss.id)
accounting.record(db, accounting.RECEIVABLE_DEBIT_KINDS[0], 40000, admin_id=seller.id)
db.commit()

rows = accounting.receivables_by_admin(db, boss)
check("the reseller does not owe themselves in their own list",
      [r["admin_id"] for r in rows], [seller.id])
check("...and their own debt is not in their total",
      accounting.receivables_total(db, boss), 40000)

# The superadmin still sees everyone below them, including that reseller.
owed_to_root = {r["admin_id"]: r["owed"] for r in accounting.receivables_by_admin(db, root)}
check("the superadmin still sees the reseller's debt", owed_to_root.get(boss.id), 65360)
check("...and the seller's", owed_to_root.get(seller.id), 40000)
check("...but never a row for themselves", root.id in owed_to_root, False)

# A level-3 seller has nobody underneath them, so the list is empty rather
# than being a list containing only themselves.
check("a seller's own receivables list is empty",
      accounting.receivables_by_admin(db, seller), [])
check("...and totals zero", accounting.receivables_total(db, seller), 0)


# --------------------------------------------------------------------------
print("\n--- the free trial ---")
db = fresh()
db.add(models.AdminUser(id=2, username="ali", hashed_password="x"))
db.add(models.Package(id=1, name="تست رایگان", quota_gb=0.2, duration_days=1, price=0,
                      is_trial=True, one_time_per_user=True, trial_daily_cap=2,
                      owner_admin_id=2))
db.add(models.Package(id=2, name="پلن عادی", quota_gb=50, duration_days=30, price=200000,
                      owner_admin_id=2))
db.commit()
trial_pkg = db.get(models.Package, 1)
paid_pkg = db.get(models.Package, 2)

check("an ordinary package is not a trial", trial.is_trial(paid_pkg), False)
check("...and the trial is", trial.is_trial(trial_pkg), True)
check("an ordinary package is never gated by any of this",
      refusal(trial.ensure_allowed, db, paid_pkg, telegram_id=555), None)

print("\nrule 1 - once per Telegram id, across every account they hold")
check("a brand-new person may take it",
      refusal(trial.ensure_allowed, db, trial_pkg, telegram_id=555), None)

first = models.User(id=10, username="a", telegram_id=555, owner_admin_id=2)
db.add(first)
db.commit()
db.add(models.Purchase(user_id=10, package_id=1, status="active"))
db.commit()
check("having taken it once, they may not again",
      "قبلاً" in str(refusal(trial.ensure_allowed, db, trial_pkg, telegram_id=555)), True)

# The dodge this rule exists for: a second account on the same Telegram id.
db.add(models.User(id=11, username="b", telegram_id=555, owner_admin_id=2))
db.commit()
check("...and a second account on the same id does not get around it",
      "قبلاً" in str(refusal(trial.ensure_allowed, db, trial_pkg,
                             user=db.get(models.User, 11), telegram_id=555)), True)
check("a different person is unaffected",
      refusal(trial.ensure_allowed, db, trial_pkg, telegram_id=999), None)

print("\nrule 2 - only for someone who has never bought")
buyer = models.User(id=20, username="c", telegram_id=777, owner_admin_id=2)
db.add(buyer)
db.commit()
db.add(models.Purchase(user_id=20, package_id=2, status="active"))   # a real purchase
db.commit()
check("an existing customer is refused",
      "خریدی نکرده" in str(refusal(trial.ensure_allowed, db, trial_pkg, telegram_id=777)), True)
# And the rule does not double up with rule 1: someone whose ONLY history
# is a trial has still never bought - rule 1 already stops them anyway, and
# the message should be the accurate one.
check("but a past trial alone is not 'has bought'",
      "قبلاً" in str(refusal(trial.ensure_allowed, db, trial_pkg, telegram_id=555)), True)

print("\nrule 3 - a daily ceiling for the whole shop")
db2 = fresh()
db2.add(models.AdminUser(id=2, username="ali", hashed_password="x"))
db2.add(models.Package(id=1, name="تست", quota_gb=0.2, duration_days=1, price=0,
                       is_trial=True, trial_daily_cap=2, owner_admin_id=2))
db2.commit()
cap_pkg = db2.get(models.Package, 1)
for n in range(2):
    u = models.User(id=100 + n, username=f"u{n}", telegram_id=1000 + n, owner_admin_id=2)
    db2.add(u)
    db2.commit()
    db2.add(models.Purchase(user_id=u.id, package_id=1, status="active",
                            created_at=dt.datetime.utcnow()))
    db2.commit()
check("two given out today", trial.trials_given_today(db2, cap_pkg), 2)
check("the third person is turned away",
      "امروز" in str(refusal(trial.ensure_allowed, db2, cap_pkg, telegram_id=2000)), True)

# Yesterday's trials must not count against today.
db2.add(models.User(id=200, username="old", telegram_id=3000, owner_admin_id=2))
db2.commit()
db2.add(models.Purchase(user_id=200, package_id=1, status="active",
                        created_at=dt.datetime.utcnow() - dt.timedelta(days=1)))
db2.commit()
check("yesterday's do not count against today", trial.trials_given_today(db2, cap_pkg), 2)

# No cap set = no ceiling.
cap_pkg.trial_daily_cap = None
db2.commit()
check("without a cap there is no ceiling",
      refusal(trial.ensure_allowed, db2, cap_pkg, telegram_id=2000), None)


print("\n--- and it costs the reseller nothing ---")
import inspect  # noqa: E402

from app.routers import bot as bot_router  # noqa: E402

src = inspect.getsource(bot_router._charge_seller)
check("the charge is skipped for a trial", "trial.is_trial(package)" in src, True)
check("...before any admin lookup, so a reseller with no credit still works",
      src.index("trial.is_trial(package)") < src.index("admin_billing.charge_for_renewal"), True)
# Both self-service doors are guarded - and only those.
check("an existing customer buying one is checked",
      "trial.ensure_allowed(db, package, user=user" in inspect.getsource(bot_router), True)
check("a brand-new customer is checked too",
      "trial.ensure_allowed(db, _new_package" in inspect.getsource(bot_router), True)
from app.routers import users as users_router  # noqa: E402
check("the admin panel's own grant is NOT - an admin may always hand one out",
      "trial.ensure_allowed" in inspect.getsource(users_router), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("نه کسی طلب خودش را صفر می‌کند، نه تست رایگان دوشیدنی است")
