"""The backlog converter: 32 live customers depend on this being right.

Run:  python3 backend/tests/test_convert_legacy_services.py

The two things that must hold: a dry run writes NOTHING, and an apply
changes no customer's quota, usage or expiry - it only moves those numbers
onto the service that owns them.
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
from app.scripts import convert_legacy_services as script

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def gb(n):
    return n * 1024 ** 3


def build():
    """A database shaped like the live one: some legacy, some already fine."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()

    node = models.Node(name="n1", type=models.NodeType.mikrotik)
    pkg = models.Package(name="۵۰ گیگ ۱ ماه", price=1, quota_gb=50, duration_days=30)
    db.add_all([node, pkg])
    db.commit()

    expiry = dt.datetime(2026, 9, 21, 12, 0)

    # 1. The reported case: one legacy service, bought through the bot.
    legacy = models.User(
        username="tg555", total_quota_bytes=gb(50), used_bytes=gb(1.5),
        expire_at=expiry, package_id=pkg.id,
    )
    db.add(legacy)
    db.commit()
    db.add(models.Connection(
        user_id=legacy.id, node_id=node.id, type=models.ConnectionType.wireguard,
        purchase_batch="batch1", package_name_snapshot=pkg.name, total_bytes=gb(1.5),
    ))

    # 2. Already converted - must be left completely alone.
    fine = models.User(username="ok", total_quota_bytes=gb(20))
    db.add(fine)
    db.commit()
    p = models.Purchase(user_id=fine.id, package_id=pkg.id, package_name_snapshot=pkg.name,
                        quota_bytes=gb(20), status=models.UserStatus.active)
    db.add(p)
    db.commit()
    db.add(models.Connection(
        user_id=fine.id, node_id=node.id, type=models.ConnectionType.wireguard, purchase_id=p.id,
    ))

    # 3. A customer with no connections at all.
    db.add(models.User(username="bare", total_quota_bytes=gb(5)))
    db.commit()
    return Session


def snapshot(db):
    return {
        u.username: (u.total_quota_bytes, u.used_bytes, u.expire_at, len(u.purchases))
        for u in db.query(models.User).all()
    }


print("--- a dry run writes nothing ---")
Session = build()
script.SessionLocal = Session
before = snapshot(Session())

sys.argv = ["convert_legacy_services"]
rc = script.main()
check("exit code 0", rc, 0)

after = snapshot(Session())
check("the database is byte-for-byte unchanged", after, before)
check("no Purchase was persisted",
      Session().query(models.Purchase).count(), 1)  # only the pre-existing one

print("\n--- apply converts, without changing any customer's numbers ---")
sys.argv = ["convert_legacy_services", "--apply"]
rc = script.main()
check("exit code 0", rc, 0)

db = Session()
u = db.query(models.User).filter_by(username="tg555").one()
check("the legacy customer now has a service", len(u.purchases), 1)
check("no connection is left on the pool",
      [c for c in u.connections if c.purchase_id is None], [])

p = u.purchases[0]
check("quota carried across unchanged", p.quota_bytes, gb(50))
check("usage carried across unchanged", p.used_bytes, gb(1.5))
check("expiry carried across unchanged", p.expire_at, dt.datetime(2026, 9, 21, 12, 0))
check("the package is remembered", p.package_name_snapshot, "۵۰ گیگ ۱ ماه")
# What the customer actually experiences must be identical.
check("what the customer sees is the same quota", u.effective_quota_bytes, gb(50))
check("...and the same expiry", u.effective_expire_at, dt.datetime(2026, 9, 21, 12, 0))

already = db.query(models.User).filter_by(username="ok").one()
check("an already-converted customer is untouched", len(already.purchases), 1)
bare = db.query(models.User).filter_by(username="bare").one()
check("a customer with no connections gains nothing", len(bare.purchases), 0)

print("\n--- running it twice is safe ---")
rc = script.main()
check("exit code 0", rc, 0)
db = Session()
u = db.query(models.User).filter_by(username="tg555").one()
check("no duplicate service was created", len(u.purchases), 1)
check("still exactly one quota", u.effective_quota_bytes, gb(50))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
