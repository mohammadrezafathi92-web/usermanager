"""Deleting a user who has an independent Purchase must not crash.

Run:  python3 backend/tests/test_delete_user_with_purchase.py

Reported 2026-09-05 from a live panel: clicking "حذف کاربر" on a user with
at least one Purchase (the normal, non-legacy service model - see
services/purchase_migration.py) failed with:

    sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) NOT NULL
    constraint failed: purchases.user_id
    [SQL: UPDATE purchases SET user_id=?, updated_at=? WHERE purchases.id = ?]

Purchase.user_id is NOT NULL, but models.Purchase's `user` relationship
(backref="purchases") had no delete cascade - SQLAlchemy's default
behaviour on deleting the parent side of a relationship with no cascade is
to disassociate children by setting their foreign key to NULL, which
violates the NOT NULL column and crashes. User.connections already used
cascade="all, delete-orphan" for exactly this reason; Purchase just never
got the same treatment when it was introduced. Fixed by adding
cascade="all, delete-orphan" to the backref.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import user_ops

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


print("--- a user with one independent Purchase and no connections ---")
db = make_db()
u = models.User(username="c1")
db.add(u)
db.commit()
db.refresh(u)
p = models.Purchase(user_id=u.id, quota_bytes=50 * 1024**3, used_bytes=0)
db.add(p)
db.commit()
purchase_id = p.id

try:
    user_ops.delete_user_cascade(db, u)
    ok = True
except Exception as exc:  # the exact crash reported - must not happen anymore
    db.rollback()
    ok = False
    print(f"        raised: {exc!r}")
check("delete succeeds instead of crashing", ok, True)
check("the user is gone", db.query(models.User).filter_by(id=u.id).count(), 0)
check("its purchase is gone too, not left dangling with a null user_id",
      db.query(models.Purchase).filter_by(id=purchase_id).count(), 0)

print("\n--- a user with a Purchase AND a connection tied to that purchase ---")
db = make_db()
node = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host="1.2.3.4")
db.add(node)
u = models.User(username="c2")
db.add(u)
db.commit()
db.refresh(node)
db.refresh(u)
p = models.Purchase(user_id=u.id, quota_bytes=0, used_bytes=0)
db.add(p)
db.commit()
db.refresh(p)
conn = models.Connection(
    # openvpn, not wireguard: deprovisioning a wireguard peer makes a real
    # network call to the MikroTik API (see services/user_ops.py's
    # deprovision_connection) - not what this test is about. OpenVPN/L2TP
    # are RADIUS-authenticated with no remote call on delete, which is all
    # that's needed to exercise the Purchase cascade this test targets.
    user_id=u.id, node_id=node.id, purchase_id=p.id,
    type=models.ConnectionType.openvpn, ppp_username="peer1",
)
db.add(conn)
db.commit()

try:
    user_ops.delete_user_cascade(db, u)
    ok = True
except Exception as exc:
    db.rollback()
    ok = False
    print(f"        raised: {exc!r}")
check("delete succeeds with a connection tied to the purchase too", ok, True)
check("the connection is gone", db.query(models.Connection).count(), 0)
check("the purchase is gone", db.query(models.Purchase).count(), 0)
check("the node itself is untouched", db.query(models.Node).count(), 1)

print("\n--- a user with TWO independent purchases (multi-service) ---")
db = make_db()
u = models.User(username="c3")
db.add(u)
db.commit()
db.refresh(u)
db.add_all([
    models.Purchase(user_id=u.id, quota_bytes=10 * 1024**3),
    models.Purchase(user_id=u.id, quota_bytes=20 * 1024**3),
])
db.commit()
check("two purchases exist before delete", db.query(models.Purchase).count(), 2)

try:
    user_ops.delete_user_cascade(db, u)
    ok = True
except Exception as exc:
    db.rollback()
    ok = False
    print(f"        raised: {exc!r}")
check("delete succeeds with multiple purchases", ok, True)
check("both purchases are gone", db.query(models.Purchase).count(), 0)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
