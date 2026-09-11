"""An account with BOTH a legacy (pre-package-migration) connection and a
currently-active Purchase must show as active, not "اتمام حجم"/"منقضی‌شده",
even when the legacy connection's own frozen quota/expiry fields are long
exhausted.

Run:  python3 backend/tests/test_mixed_legacy_purchase_status.py

Reported 2026-09: "اکانتی که هنوز یکی از پکیج‌هاش اعتبار داره زده اتمام
حجم". _user_status_from_purchases already handled this correctly for an
account made ENTIRELY of Purchases (see its own docstring - this exact
confusion was fixed there on 2026-08-10), but a MIXED account - one still
carrying an old legacy connection alongside a newer Purchase - fell
straight through to the legacy-only branch of _enforce_user_limits, which
set user.status purely from the stale legacy fields and never looked at
user.purchases at all.

Also verifies the other half doesn't regress: the legacy connection itself
must still get disabled by its own exhausted quota, regardless of the
valid purchase - a purchase being fine must never re-enable an unrelated,
genuinely-exhausted legacy service."""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, selectinload

from app import models
from app.services import quota_manager

failures: list[str] = []
GB = 1024 ** 3
NOW = dt.datetime.utcnow()

calls: list[tuple[int, bool]] = []


def _fake_set_connection_enabled(db, connection, enabled):
    """Same network-free stub test_concurrent_session_kick.py uses - this
    file is about the STATUS decision, not the MikroTik/Xray API calls
    (those are covered by test_connection_toggle_no_silent_success.py)."""
    calls.append((connection.id, enabled))
    connection.enabled = enabled


quota_manager._set_connection_enabled = _fake_set_connection_enabled


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    node = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host="1.2.3.4")
    pkg = models.Package(name="p", quota_gb=50, price=1)
    db.add_all([node, pkg])
    db.commit()
    return db, node, pkg


print("--- mixed account: exhausted legacy service + still-active purchase ---")
db, node, pkg = make_db()
u = models.User(username="mixed1", total_quota_bytes=10 * GB, used_bytes=10 * GB)  # legacy: exhausted
db.add(u)
db.commit()
db.refresh(u)

legacy_conn = models.Connection(user_id=u.id, node_id=node.id, type=models.ConnectionType.wireguard,
                                 wg_peer_name="mixed1-legacy", purchase_id=None, enabled=True)
db.add(legacy_conn)

purchase = models.Purchase(user_id=u.id, package_id=pkg.id, status=models.UserStatus.active,
                            quota_bytes=50 * GB, used_bytes=1 * GB,
                            expire_at=NOW + dt.timedelta(days=30))
db.add(purchase)
db.commit()
db.refresh(purchase)

purchase_conn = models.Connection(user_id=u.id, node_id=node.id, type=models.ConnectionType.wireguard,
                                   wg_peer_name="mixed1-pkg", purchase_id=purchase.id, enabled=True)
db.add(purchase_conn)
db.commit()

user_full = (
    db.query(models.User)
    .options(selectinload(models.User.connections), selectinload(models.User.purchases))
    .filter(models.User.id == u.id)
    .first()
)
quota_manager._enforce_user_limits(db, user_full)
db.commit()
db.refresh(u)
db.refresh(legacy_conn)

check("account badge stays ACTIVE thanks to the valid purchase", u.status, models.UserStatus.active)
check("the legacy connection is still disabled (its OWN quota is exhausted)",
      legacy_conn.enabled, False)

print("\n--- same shape, but the purchase is ALSO exhausted: badge follows both ---")
db2, node2, pkg2 = make_db()
u2 = models.User(username="mixed2", total_quota_bytes=10 * GB, used_bytes=10 * GB)
db2.add(u2)
db2.commit()
db2.refresh(u2)
legacy_conn2 = models.Connection(user_id=u2.id, node_id=node2.id, type=models.ConnectionType.wireguard,
                                  wg_peer_name="mixed2-legacy", purchase_id=None, enabled=True)
db2.add(legacy_conn2)
purchase2 = models.Purchase(user_id=u2.id, package_id=pkg2.id, status=models.UserStatus.quota_exceeded,
                             quota_bytes=50 * GB, used_bytes=60 * GB,
                             expire_at=NOW + dt.timedelta(days=30))
db2.add(purchase2)
db2.commit()
db2.refresh(u2)

user2_full = (
    db2.query(models.User)
    .options(selectinload(models.User.connections), selectinload(models.User.purchases))
    .filter(models.User.id == u2.id)
    .first()
)
quota_manager._enforce_user_limits(db2, user2_full)
db2.commit()
db2.refresh(u2)
check("with NO valid purchase either, the badge correctly shows exhausted",
      u2.status, models.UserStatus.quota_exceeded)

print("\n--- pure legacy account (no purchases at all): unaffected by this fix ---")
db3, node3, pkg3 = make_db()
u3 = models.User(username="legacy_only", total_quota_bytes=10 * GB, used_bytes=1 * GB)
db3.add(u3)
db3.commit()
db3.refresh(u3)
conn3 = models.Connection(user_id=u3.id, node_id=node3.id, type=models.ConnectionType.wireguard,
                           wg_peer_name="legacy_only-c", purchase_id=None, enabled=False)
db3.add(conn3)
db3.commit()
user3_full = (
    db3.query(models.User)
    .options(selectinload(models.User.connections), selectinload(models.User.purchases))
    .filter(models.User.id == u3.id)
    .first()
)
quota_manager._enforce_user_limits(db3, user3_full)
db3.commit()
db3.refresh(u3)
db3.refresh(conn3)
check("a healthy pure-legacy account is active", u3.status, models.UserStatus.active)
check("...and its connection gets (re)enabled", conn3.enabled, True)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("وضعیت حساب حالا حتی وقتی سرویس قدیمی تمام شده، پکیج معتبر را نادیده نمی‌گیرد")
