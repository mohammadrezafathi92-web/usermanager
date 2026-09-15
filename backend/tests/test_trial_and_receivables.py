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
import pathlib
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


# --------------------------------------------------------------------------
print("\n--- a free package asks for no payment ---")
# «برای تست رایگان گزینه پرداخت نباید بیاد چه بات چه مینی اپ» - and the two
# ways it used to: the bot showed a card number for a zero-toman package
# (final_price of 0 is falsy, so the "pay from balance" branch was skipped
# and the card branch ran), and the Mini App opened a checkout sheet.
import inspect as _inspect  # noqa: E402

from app.telegram_bot.handlers import customer as _customer  # noqa: E402

_pay = _inspect.getsource(_customer._show_payment_screen)
check("the bot leaves before the card block when there is nothing to pay",
      "if final_price <= 0:" in _pay, True)
check("...and hands it to the approval path rather than provisioning inline",
      "perform_approval(pending, bot)" in _inspect.getsource(_customer._give_free_package), True)

_mini = (_pathlib_root := pathlib.Path(__file__).resolve().parents[2]) / "frontend" / "src" / "pages" / "MiniApp.jsx"
_m = _mini.read_text(encoding="utf-8")
check("the Mini App shows a claim button, not a price row", "دریافت رایگان" in _m, True)
check("...and does not open the checkout sheet for it", "onBuy(pkg, { free: true })" in _m, True)
# There is no separate price row any more - the price lives inside the buy
# button (see PackagePicker) - so "free" is expressed by that button being a
# different button, with no toman figure on it at all.
_free_btn = _m[_m.index("{buyable && free ?"):_m.index(") : buyable ?")]
check("the free button carries no price", "تومان" in _free_btn, False)
check("...and it is the free one", "دریافت رایگان" in _free_btn, True)


print("\n--- the price floor does not apply to something meant to be free ---")
from app.routers import packages as _pr  # noqa: E402

_create = _inspect.getsource(_pr.create_package)
check("a trial skips the floor", "if payload.is_trial:" in _create, True)
check("...and everything else still gets it", "_check_price_floor(" in _create, True)


print("\n--- what a reseller may NOT do to a trial ---")
db3 = fresh()
boss3 = models.AdminUser(id=2, username="ali", hashed_password="x", wholesale_price_per_gb=2400)
root3 = models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True)
db3.add_all([boss3, root3])
db3.commit()

from app import schemas as _schemas  # noqa: E402

made = _pr.create_package(
    _schemas.PackageCreate(name="تست", quota_gb=0.2, duration_days=1, price=0, is_trial=True),
    db=db3, admin=boss3,
)
check("a free trial can be created at all despite the per-GB floor", made.price, 0)

for label, change in (
    ("grow the quota", {"quota_gb": 500}),
    ("grow the duration", {"duration_days": 365}),
    ("give it a price", {"price": 50000}),
):
    detail = refusal(_pr.update_package, made.id, _schemas.PackageUpdate(**change), db=db3, admin=boss3)
    check(f"{label} is refused", bool(detail), True)

# The subtler one: ticking is_trial ON an existing large package would stop
# it being charged for - a way to give away a year of service for nothing.
big = _pr.create_package(
    _schemas.PackageCreate(name="بزرگ", quota_gb=500, duration_days=365, price=9_000_000),
    db=db3, admin=boss3,
)
check("turning a big package INTO a trial is refused",
      bool(refusal(_pr.update_package, big.id, _schemas.PackageUpdate(is_trial=True), db=db3, admin=boss3)),
      True)

# The superadmin is exempt - it is their platform and their cost.
check("a superadmin may set a larger trial",
      _pr.create_package(
          _schemas.PackageCreate(name="تست بزرگ", quota_gb=50, duration_days=30, price=0, is_trial=True),
          db=db3, admin=root3,
      ).quota_gb,
      50)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("نه کسی طلب خودش را صفر می‌کند، نه تست رایگان دوشیدنی است")
