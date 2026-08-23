"""قفل خرید - the till closes, the service keeps running.

Run:  python3 backend/tests/test_purchase_lock.py

The whole point of this feature is the difference between it and
disabling an account. Disabling cuts the customer off; this stops them
buying anything new while everything they already paid for keeps working
to the end of its term.

So the tests come in two halves, and the second half matters more than
the first: it is easy to block a purchase, and easy to break a working
service by accident while doing it.
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

from app import models, schemas
from app.routers import bot as bot_router
from app.routers import users as users_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def build(blocked=False, reason=None):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    admin = models.AdminUser(username="su", hashed_password="x", is_superadmin=True)
    pkg = models.Package(name="۵۰ گیگ", quota_gb=50, price=90_000, enabled=True)
    db.add_all([admin, pkg])
    db.commit()
    db.refresh(admin)
    db.refresh(pkg)

    user = models.User(
        username="cust1", telegram_id=555001, balance=200_000,
        total_quota_bytes=50 * 1024 ** 3, used_bytes=10 * 1024 ** 3,
        expire_at=dt.datetime.utcnow() + dt.timedelta(days=20),
        status=models.UserStatus.active, owner_admin_id=admin.id,
        purchases_blocked=blocked,
        purchases_blocked_reason=reason,
        purchases_blocked_at=dt.datetime.utcnow() if blocked else None,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return db, admin, pkg, user


def refused(fn):
    """(was_it_refused, message)."""
    try:
        fn()
        return False, None
    except HTTPException as exc:
        return True, str(exc.detail)


REASON = "تسویه‌حساب انجام نشده - با پشتیبانی تماس بگیرید"

print("--- the till is closed ---")
db, admin, pkg, user = build(blocked=True, reason=REASON)

was, msg = refused(lambda: bot_router.purchase_package(
    "cust1", schemas.BotPurchasePackageRequest(package_id=pkg.id), db=db))
check("cannot buy a new service", was, True)
check("...and is told the admin's own reason", msg, REASON)

was, _ = refused(lambda: bot_router.renew(
    "cust1", schemas.BotRenewRequest(package_id=pkg.id, add_gb=50, add_days=30), db=db))
check("cannot renew", was, True)

was, _ = refused(lambda: bot_router.add_balance(
    "cust1", schemas.BotAddBalanceRequest(amount=50_000), db=db))
check("cannot top up the wallet", was, True)

was, _ = refused(lambda: bot_router.create_user(
    schemas.BotCreateUserRequest(username="cust1_second", telegram_id=555001), db=db))
check("cannot open a second account on the same Telegram id", was, True)

# A different person is unaffected - the lock is on this customer, not on
# the panel.
was, _ = refused(lambda: bot_router.create_user(
    schemas.BotCreateUserRequest(username="somebody_else", telegram_id=999999), db=db))
check("a different Telegram id can still sign up", was, False)

print("\n--- with no reason typed, a sensible default is shown ---")
db2, _, pkg2, _ = build(blocked=True, reason=None)
was, msg = refused(lambda: bot_router.purchase_package(
    "cust1", schemas.BotPurchasePackageRequest(package_id=pkg2.id), db=db2))
check("still refused", was, True)
check("...with the default wording", msg, bot_router.DEFAULT_PURCHASE_BLOCK_MESSAGE)

print("\n--- but the service they already have is untouched ---")
db, admin, pkg, user = build(blocked=True, reason=REASON)
check("still active, not disabled", user.status, models.UserStatus.active)
check("quota intact", user.total_quota_bytes, 50 * 1024 ** 3)
check("expiry intact", user.expire_at is not None, True)
check("wallet balance intact", user.balance, 200_000)

# Reading their account, configs and usage must all still work - a locked
# customer still needs to use what they bought.
me = bot_router.get_user_by_telegram(555001, db=db)
check("the bot can still read their account", me.username, "cust1")
check("...and reports the lock so the buttons can explain it", me.purchases_blocked, True)
check("...carrying the reason", me.purchases_blocked_reason, REASON)

# Spending the wallet is part of a purchase, which is already refused -
# but if a debit ever arrives it must not be blocked, or the customer
# would pay and get nothing.
was, _ = refused(lambda: bot_router.add_balance(
    "cust1", schemas.BotAddBalanceRequest(amount=-1000), db=db))
check("a wallet DEBIT is not blocked (never take money for nothing)", was, False)

print("\n--- nothing in the enforcement path may read the flag ---")
# This is what makes "the current service keeps working" a property of the
# code rather than a hope. If any of these ever learns about the lock, a
# locked customer starts getting cut off mid-term.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for module in ["services/radius_server.py", "services/quota_manager.py",
               "services/xray_client.py", "services/mikrotik_client.py",
               "routers/subscription.py"]:
    text = open(os.path.join(ROOT, "app", module), encoding="utf-8").read()
    check(f"{module} does not consult purchases_blocked", "purchases_blocked" in text, False)

print("\n--- the panel is deliberately NOT blocked ---")
# An admin must still be able to sort out a locked customer - that is the
# whole reason the lock lives on the bot API and not on the model.
db, admin, pkg, user = build(blocked=True, reason=REASON)
was, _ = refused(lambda: users_router.apply_package(
    user.id, schemas.ApplyPackageRequest(package_id=pkg.id), db=db, admin=admin))
check("an admin can still add a package from the panel", was, False)

print("\n--- unlocking ---")
db, admin, pkg, user = build(blocked=True, reason=REASON)
users_router.update_user(
    user.id, schemas.UserUpdate(purchases_blocked=False), db=db, admin=admin)
db.refresh(user)
check("the lock is off", user.purchases_blocked, False)
check("...the reason is cleared, not left to be shown later", user.purchases_blocked_reason, None)
check("...and so is the date", user.purchases_blocked_at, None)
was, _ = refused(lambda: bot_router.purchase_package(
    "cust1", schemas.BotPurchasePackageRequest(package_id=pkg.id), db=db))
check("they can buy again", was, False)

print("\n--- locking from the panel stamps the date ---")
db, admin, pkg, user = build(blocked=False)
users_router.update_user(
    user.id, schemas.UserUpdate(purchases_blocked=True, purchases_blocked_reason=REASON),
    db=db, admin=admin)
db.refresh(user)
check("locked", user.purchases_blocked, True)
check("...reason saved", user.purchases_blocked_reason, REASON)
check("...date stamped", user.purchases_blocked_at is not None, True)
check("...status NOT changed as a side effect", user.status, models.UserStatus.active)

print("\n--- an untouched customer behaves exactly as before ---")
db, admin, pkg, user = build(blocked=False)
check("default is unlocked", bool(user.purchases_blocked), False)
was, _ = refused(lambda: bot_router.add_balance(
    "cust1", schemas.BotAddBalanceRequest(amount=50_000), db=db))
check("can top up", was, False)
was, _ = refused(lambda: bot_router.purchase_package(
    "cust1", schemas.BotPurchasePackageRequest(package_id=pkg.id), db=db))
check("can buy", was, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
