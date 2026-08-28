"""Answers a direct question from the panel owner: after a renewal that got
QUEUED (because the service was still active when it was requested - see
user_ops.renew_purchase's docstring), does the leftover usage from the OLD
cycle actually get wiped once the queue kicks in, or does it linger?

test_poll_equivalence.py already proves the ACTIVATION half (a Purchase
built directly with reserved_quota_bytes/reserved_duration_days already set
gets used_bytes reset to 0 and quota/expiry raised once it's exhausted).
What it does NOT cover is the QUEUING half - does calling the real
renew_purchase(), the function the panel's "تمدید" button and the bot both
actually call, produce that same reserved state in the first place? This
file wires both halves together through the real public functions (not
hand-built Purchase rows), end to end, exactly as it happens for a real
admin renewing a real customer's still-active service.

Run:  python3 backend/tests/test_renew_reservation_roundtrip.py
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, selectinload

from app import models
from app.services import quota_manager, user_ops

failures: list[str] = []
GB = 1024 ** 3
NOW = dt.datetime.utcnow()


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def build():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    admin = models.AdminUser(username="su", hashed_password="x", is_superadmin=True)
    node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
    db.add_all([admin, node])
    db.commit()
    user = models.User(username="cust1", owner_admin_id=admin.id, total_quota_bytes=0, used_bytes=0)
    db.add(user)
    db.commit()
    db.refresh(user)

    # A service that is STILL ACTIVE - 20 of 50GB used, 25 of 30 days left.
    # This is the exact shape that makes renew_purchase() queue instead of
    # applying immediately (see its docstring).
    purchase = models.Purchase(
        user_id=user.id, package_id=None, package_name_snapshot="۵۰ گیگ ماهانه",
        quota_bytes=50 * GB, used_bytes=20 * GB,
        expire_at=NOW + dt.timedelta(days=25),
        status=models.UserStatus.active,
    )
    db.add(purchase)
    db.commit()
    db.refresh(purchase)

    conn = models.Connection(
        user_id=user.id, node_id=node.id, purchase_id=purchase.id,
        type="l2tp", ppp_username="cust1", ppp_password="x", enabled=True,
    )
    db.add(conn)
    db.commit()
    return db, user, purchase


print("--- step 1: renewing a still-active service via the real endpoint function ---")
db, user, purchase = build()
before_used, before_quota, before_expire = purchase.used_bytes, purchase.quota_bytes, purchase.expire_at

result = user_ops.renew_purchase(db, purchase, add_gb=100, add_days=30)

check("this is the SAME row (queued, not a new one)", result.id, purchase.id)
check("used_bytes is untouched for now - the current cycle still has 30GB left, unwise to erase it early",
      purchase.used_bytes, before_used)
check("quota_bytes is untouched for now too", purchase.quota_bytes, before_quota)
check("expire_at is untouched for now too", purchase.expire_at, before_expire)
check("the renewal IS recorded - just queued, in reserved_quota_bytes", purchase.reserved_quota_bytes, 100 * GB)
check("...and in reserved_duration_days", purchase.reserved_duration_days, 30)
check("status stays active - nothing about this customer's access changed", purchase.status, models.UserStatus.active)

print("\n--- step 2: time passes, the customer actually uses up the old cycle ---")
purchase.used_bytes = 50 * GB  # they hit their old 50GB cap
db.commit()

print("--- step 3: the real poll cycle's enforcement function runs (as it does every 30s) ---")
quota_manager._enforce_purchase_limits(db, purchase)
db.commit()
db.refresh(purchase)

check("the queued renewal activated: used_bytes is wiped to 0 (this is the customer's literal question)",
      purchase.used_bytes, 0)
check("quota_bytes became the NEW 100GB that was queued, not a leftover 50GB", purchase.quota_bytes, 100 * GB)
check("expire_at is recomputed fresh from now + the queued 30 days, not stacked onto the old date",
      abs((purchase.expire_at - (dt.datetime.utcnow() + dt.timedelta(days=30))).total_seconds()) < 5, True)
check("the reservation is consumed - nothing left queued", purchase.reserved_quota_bytes, None)
check("...same for the queued days", purchase.reserved_duration_days, None)
check("status is active again (it would have flipped to quota_exceeded first, then been reconciled)",
      purchase.status, models.UserStatus.active)

db.refresh(user)
conn = db.query(models.Connection).filter(models.Connection.purchase_id == purchase.id).first()
check("the connection itself is still enabled - the customer never lost access mid-renewal",
      conn.enabled, True)

print("\n--- for comparison: renewing an ALREADY-exhausted service applies immediately, no queue ---")
db2, user2, purchase2 = build()
purchase2.used_bytes = 50 * GB  # already at the cap
db2.commit()
user_ops.renew_purchase(db2, purchase2, add_gb=100, add_days=30)
check("no queueing needed - it went straight to the new values", purchase2.used_bytes, 0)
check("...quota too", purchase2.quota_bytes, 100 * GB)
check("...and nothing is sitting in reserved_* waiting for later", purchase2.reserved_quota_bytes, None)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
