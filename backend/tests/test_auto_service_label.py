"""services/user_ops.py's _auto_service_label - a Purchase whose customer
did not type their own label at purchase time now gets a sequential
"اکانت N" fallback instead of staying blank forever.

Run:  python3 backend/tests/test_auto_service_label.py

Feature requested 2026-09-09: a customer (or admin, looking from
UserDetail.jsx) with several unlabeled services had no way to tell them
apart - every one showed as a blank "افزودن یادداشت" in the panel and
nothing at all on the public subscription page (schemas.
SubscriptionConnectionOut.comment). Also fixes a related gap found while
building this: routers/bot.py's create_user (the brand-new-customer signup
path, called from admin_pending.py's receipt-approval flow) had no
`comment` field on its request schema at all, so a customer's own typed
label on their VERY FIRST purchase was silently dropped even though the
bot already asked for one - see schemas.BotCreateUserRequest.comment and
absorb_legacy_pool_into_purchase's new `comment` parameter below.

Uses direct models.Connection rows instead of the real provision_connection
(which talks to a live MikroTik/Xray node over the network) to simulate a
customer's legacy pool - same approach other tests in this suite use for
anything that would otherwise need live node infrastructure."""
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

pkg = packages_router.create_package(
    schemas.PackageCreate(name="پکیج تست", quota_gb=1, duration_days=30, price=1000),
    db=db, admin=admin,
)
node = models.Node(name="node1", type=models.NodeType.mikrotik)
db.add(node)
db.commit()
db.refresh(node)

user1 = user_ops.create_user_record(db, "cust1", telegram_id=111, package_id=pkg.id)
db.commit()

print("--- absorb_legacy_pool_into_purchase with NO comment -> auto-labeled 'اکانت 1' ---")
db.add(models.Connection(user_id=user1.id, node_id=node.id, type=models.ConnectionType.wireguard, enabled=True))
db.commit()
p1 = user_ops.absorb_legacy_pool_into_purchase(db, user1)
db.commit()
check("purchase created", p1 is not None, True)
check("auto-labeled 'اکانت 1'", p1.comment, "اکانت 1")

print("\n--- an explicit comment on the FIRST purchase is honored (the bug this fixes) ---")
user2 = user_ops.create_user_record(db, "cust2", telegram_id=222, package_id=pkg.id)
db.commit()
db.add(models.Connection(user_id=user2.id, node_id=node.id, type=models.ConnectionType.wireguard, enabled=True))
db.commit()
p2 = user_ops.absorb_legacy_pool_into_purchase(db, user2, comment="گوشی شخصی")
db.commit()
check("customer's own typed label is kept, not overwritten by the fallback", p2.comment, "گوشی شخصی")

print("\n--- routers/bot.py's create_user now HAS a comment field and forwards it verbatim ---")
check(
    "BotCreateUserRequest carries the customer's typed label through to absorb_legacy_pool_into_purchase",
    schemas.BotCreateUserRequest(username="x", comment="اکانت اداری").comment,
    "اکانت اداری",
)

print("\n--- purchase_package: second purchase (same customer) with no comment -> 'اکانت 2' ---")
bot_router.purchase_package("cust1", schemas.BotPurchasePackageRequest(package_id=pkg.id), db=db)
db.commit()
purchases = db.query(models.Purchase).filter(models.Purchase.user_id == user1.id).order_by(models.Purchase.id).all()
check("two purchases exist for cust1", len(purchases), 2)
check("first purchase (the absorbed legacy pool) is 'اکانت 1'", purchases[0].comment, "اکانت 1")
check("second purchase auto-labeled 'اکانت 2'", purchases[1].comment, "اکانت 2")

print("\n--- an explicit comment on purchase_package is always honored over the auto-label ---")
bot_router.purchase_package(
    "cust1", schemas.BotPurchasePackageRequest(package_id=pkg.id, comment="سرویس لپ‌تاپ"), db=db,
)
db.commit()
purchases2 = db.query(models.Purchase).filter(models.Purchase.user_id == user1.id).order_by(models.Purchase.id).all()
check("three purchases exist for cust1", len(purchases2), 3)
check("third purchase keeps its own typed label", purchases2[2].comment, "سرویس لپ‌تاپ")

print("\n--- the admin panel's own apply_package (no comment param at all) is also auto-labeled ---")
users_router.apply_package(user1.id, schemas.ApplyPackageRequest(package_id=pkg.id), db=db, admin=admin)
db.commit()
purchases3 = db.query(models.Purchase).filter(models.Purchase.user_id == user1.id).order_by(models.Purchase.id).all()
check("four purchases exist for cust1", len(purchases3), 4)
check("admin-granted purchase auto-labeled 'اکانت 4'", purchases3[3].comment, "اکانت 4")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
