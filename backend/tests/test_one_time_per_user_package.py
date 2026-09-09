"""models.Package.one_time_per_user - routers/bot.py's customer-facing
purchase endpoints (create_user / purchase_package) must refuse a repeat
purchase of a package flagged one_time_per_user=True, checked across every
User row sharing the same Telegram id (not just the one username being
bought under). routers/users.py's admin-panel apply_package must NEVER be
restricted by this flag - an admin/seller can always manually re-grant it.

Run:  python3 backend/tests/test_one_time_per_user_package.py

Feature requested 2026-09-09: some packages are trial/heavily-discounted
and an admin does not want the same customer buying them more than once
through the bot."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from fastapi import HTTPException  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models, schemas  # noqa: E402
from app.routers import packages as packages_router  # noqa: E402
from app.routers import bot as bot_router  # noqa: E402
from app.routers import users as users_router  # noqa: E402
from app.services import hierarchy, user_ops  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin = models.AdminUser(username="admin1", hashed_password="x", is_superadmin=True, role="superadmin")
db.add(admin)
db.commit()
db.refresh(admin)
hierarchy.rebuild_path(db, admin)
db.commit()

one_time_pkg = packages_router.create_package(
    schemas.PackageCreate(name="پکیج تست", quota_gb=1, duration_days=30, price=1000, one_time_per_user=True),
    db=db, admin=admin,
)
normal_pkg = packages_router.create_package(
    schemas.PackageCreate(name="پکیج عادی", quota_gb=1, duration_days=30, price=1000),
    db=db, admin=admin,
)
check("one_time_pkg persisted the flag", one_time_pkg.one_time_per_user, True)
check("normal_pkg defaults to False", normal_pkg.one_time_per_user, False)

print("\n--- a normal (non one-time) package can be bought by the same user twice ---")
user1 = user_ops.create_user_record(db, "user1", telegram_id=111)
db.commit()
bot_router.purchase_package(
    "user1", schemas.BotPurchasePackageRequest(package_id=normal_pkg.id), db=db,
)
db.commit()
bot_router.purchase_package(
    "user1", schemas.BotPurchasePackageRequest(package_id=normal_pkg.id), db=db,
)
db.commit()
check(
    "user1 has two purchases of the normal package",
    db.query(models.Purchase).filter(models.Purchase.user_id == user1.id, models.Purchase.package_id == normal_pkg.id).count(),
    2,
)

print("\n--- first purchase of the one-time package succeeds ---")
bot_router.purchase_package(
    "user1", schemas.BotPurchasePackageRequest(package_id=one_time_pkg.id), db=db,
)
db.commit()
check(
    "user1 now has one purchase of the one-time package",
    db.query(models.Purchase).filter(models.Purchase.user_id == user1.id, models.Purchase.package_id == one_time_pkg.id).count(),
    1,
)

print("\n--- buying it again (same user) is rejected ---")
try:
    bot_router.purchase_package(
        "user1", schemas.BotPurchasePackageRequest(package_id=one_time_pkg.id), db=db,
    )
    failures.append("second purchase should have raised")
    print("FAIL  second purchase should have raised HTTPException")
except HTTPException as exc:
    check("second purchase rejected with 403", exc.status_code, 403)
db.rollback()

print("\n--- a DIFFERENT customer (different telegram id) can still buy it once ---")
user2 = user_ops.create_user_record(db, "user2", telegram_id=222)
db.commit()
bot_router.purchase_package(
    "user2", schemas.BotPurchasePackageRequest(package_id=one_time_pkg.id), db=db,
)
db.commit()
check(
    "user2 has one purchase of the one-time package",
    db.query(models.Purchase).filter(models.Purchase.user_id == user2.id, models.Purchase.package_id == one_time_pkg.id).count(),
    1,
)

print("\n--- cannot dodge the limit with a SECOND account on the SAME telegram id ---")
user1b = user_ops.create_user_record(db, "user1-second-account", telegram_id=111)
db.commit()
try:
    bot_router.purchase_package(
        "user1-second-account", schemas.BotPurchasePackageRequest(package_id=one_time_pkg.id), db=db,
    )
    failures.append("sibling-account purchase should have raised")
    print("FAIL  sibling-account purchase should have raised HTTPException")
except HTTPException as exc:
    check("sibling-account purchase rejected with 403", exc.status_code, 403)
db.rollback()

print("\n--- create_user() also blocks a brand-new signup on an already-used telegram id ---")
try:
    bot_router.create_user(
        schemas.BotCreateUserRequest(username="user1-third-account", telegram_id=111, package_id=one_time_pkg.id),
        db=db,
    )
    failures.append("create_user with reused telegram id should have raised")
    print("FAIL  create_user should have raised HTTPException")
except HTTPException as exc:
    check("create_user rejected with 403", exc.status_code, 403)
db.rollback()
check(
    "no stray user row was created by the rejected create_user call",
    db.query(models.User).filter(models.User.username == "user1-third-account").first(),
    None,
)

print("\n--- but create_user() for a FRESH telegram id still works fine ---")
resp = bot_router.create_user(
    schemas.BotCreateUserRequest(username="user3", telegram_id=333, package_id=one_time_pkg.id),
    db=db,
)
db.commit()
check("user3 created successfully", resp.username, "user3")

print("\n--- the admin panel's own apply_package (manual grant) is NEVER restricted by this flag ---")
# user1 already has a Purchase of one_time_pkg from above - an admin must
# still be able to grant it again by hand.
purchase = users_router.apply_package(
    user1.id, schemas.ApplyPackageRequest(package_id=one_time_pkg.id), db=db, admin=admin,
)
db.commit()
check(
    "admin could manually re-grant the one-time package to user1",
    db.query(models.Purchase).filter(models.Purchase.user_id == user1.id, models.Purchase.package_id == one_time_pkg.id).count(),
    2,
)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
