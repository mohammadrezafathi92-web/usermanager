"""The manifest's loyalty quota target equals where T_final grants it.

Run:  python3 backend/tests/test_loyalty_quota_binding.py

Bug (production, 2026-11-09): every receipt-approved NEW service for an
EXISTING customer that earned a loyalty quota reward was compensated with
final_tx_failed_permanent (25 sales). The manifest projected the reward
onto the customer's one service BEFORE the sale (Purchase 1209); at
T_final the new service exists too, two services have no single finite
destination, so nothing was granted - a required effect missing, the
whole sale rolled back. The branch meant for this case tested
shape == "purchase", a shape that does not exist ("existing_user").
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "loyalty-quota-binding-test-only-0123456789")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import quota_rewards, receipt_approval_intent as ri

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine, autoflush=False)()
GB = 1024 ** 3
pkg = models.Package(name="30g", quota_gb=30, duration_days=30, price=240_000)
db.add(pkg); db.flush()


def customer(name, purchases, pool=0):
    user = models.User(username=name, total_quota_bytes=pool, status=models.UserStatus.active)
    db.add(user); db.flush()
    for quota in purchases:
        db.add(models.Purchase(user_id=user.id, package_id=pkg.id, quota_bytes=quota, status=models.UserStatus.active))
    db.flush()
    return user


def target(shape, user):
    return ri.Target(shape=shape, owner_admin_id=None, tenant_scope_key="t", telegram_id=None,
                     telegram_id_source="none", user_id=user.id)


def at_final(user, new_purchase: bool):
    """Where loyalty_service grants it: quota_rewards.destination after the sale."""
    if new_purchase:
        db.add(models.Purchase(user_id=user.id, package_id=pkg.id, quota_bytes=30 * GB, status=models.UserStatus.active))
    found = quota_rewards.destination(db, user)
    if found is None:
        result = None
    elif isinstance(found, models.Purchase):
        result = "new purchase" if new_purchase and found.id not in old_ids(user) else {"type": "Purchase", "id": found.id}
    else:
        result = {"type": "User", "id": user.id}
    db.rollback()
    return result


_ids = {}


def old_ids(user):
    return _ids[user.id]


def projected(shape, user):
    value = ri._loyalty_quota_binding(db, target(shape, user), pkg, True, 30 * GB)
    return "new purchase" if value == ri.PURCHASE_REF else value


cases = [
    ("existing customer with one service buys a second one", customer("one", [30 * GB]), "existing_user", True),
    ("existing customer with no service buys one", customer("none", []), "existing_user", True),
    ("existing customer with only a legacy pool buys a service", customer("pool", [], pool=20 * GB), "existing_user", True),
    ("existing customer with two services buys a third", customer("two", [30 * GB, 30 * GB]), "existing_user", True),
    ("renewal of the one service", customer("renew", [30 * GB]), "renew_purchase", False),
]
db.commit()
for label, user, shape, creates in cases:
    _ids[user.id] = {p.id for p in db.query(models.Purchase).filter_by(user_id=user.id)}
    check(f"{label}: manifest == T_final", projected(shape, user), at_final(user, creates))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall loyalty quota binding checks passed")
