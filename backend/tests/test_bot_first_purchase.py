"""A customer's FIRST purchase through the bot must be a real service.

Run:  python3 backend/tests/test_bot_first_purchase.py

Reported from the live panel: an account bought minutes earlier showed
"سرویس اشتراکی (قدیمی)" with a convert button. The cause was that the
brand-new-customer path created connections without a Purchase, while the
existing-customer path created one - so the first purchase was always
legacy and only the second onwards was correct.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import bot as bot_router
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


# provision_connection talks to real nodes; replace it with a recorder that
# creates the row the way the real one does. What is under test is whether a
# Purchase gets created and linked, not the provisioning itself.
def fake_provision(db, user, node, protocol, flow="", max_sessions=1,
                   purchase_batch=None, package_name=None, speed_limit_mbps=None):
    conn = models.Connection(
        user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard,
        purchase_batch=purchase_batch, package_name_snapshot=package_name,
    )
    db.add(conn)
    db.flush()
    user.connections.append(conn)
    return conn


user_ops.provision_connection = fake_provision
bot_router.user_ops.provision_connection = fake_provision

# The endpoint's RESPONSE builder asks each node for the live config text,
# which needs a real router. Stubbed for the same reason as above: what is
# under test is the rows written, not how they are rendered back.
user_ops.get_connection_share = lambda conn: {}
bot_router.user_ops.get_connection_share = lambda conn: {}


def setup():
    db = make_db()
    node = models.Node(name="n1", type=models.NodeType.mikrotik)
    pkg = models.Package(name="۵۰ گیگ ۱ ماه", price=100_000, quota_gb=50, duration_days=30)
    db.add_all([node, pkg])
    db.commit()
    db.refresh(node)
    db.refresh(pkg)
    return db, node, pkg


def buy_new(db, node, pkg, username):
    payload = schemas.BotCreateUserRequest(
        username=username, full_name="Pouya Kaveh",
        quota_gb=pkg.quota_gb, expire_days=pkg.duration_days,
        telegram_id=555, package_id=pkg.id, package_name=pkg.name,
        connections=[schemas.BotCreateConnectionSpec(node_id=node.id, protocol="wireguard")],
    )
    return bot_router.create_user(payload, db=db)


print("--- a brand-new customer's first purchase ---")
db, node, pkg = setup()
buy_new(db, node, pkg, "tg555")
user = db.query(models.User).filter_by(username="tg555").one()

check("a Purchase was created", len(user.purchases), 1)
check("no connection is left on the shared pool",
      [c for c in user.connections if c.purchase_id is None], [])
check("the connection is linked to that Purchase",
      user.connections[0].purchase_id, user.purchases[0].id)

p = user.purchases[0]
check("the Purchase carries the package name", p.package_name_snapshot, "۵۰ گیگ ۱ ماه")
check("the Purchase carries the package id", p.package_id, pkg.id)
check("quota came across exactly once",
      (p.quota_bytes, user.effective_quota_bytes), (user.total_quota_bytes, user.total_quota_bytes))
check("expiry came across", p.expire_at, user.expire_at)
check("the service is active", p.status, models.UserStatus.active)

print("\n--- which is what the panel reads to decide 'legacy' ---")
# UserDetail.jsx: legacyConns = connections.filter(c => !c.purchase_id)
legacy = [c for c in user.connections if c.purchase_id is None]
check("nothing renders as «سرویس اشتراکی (قدیمی)»", len(legacy), 0)

print("\n--- a purchase with no connections at all ---")
db, node, pkg = setup()
payload = schemas.BotCreateUserRequest(
    username="empty", quota_gb=10, expire_days=30,
    package_id=pkg.id, package_name=pkg.name, connections=[],
)
bot_router.create_user(payload, db=db)
user = db.query(models.User).filter_by(username="empty").one()
# Nothing was provisioned, so there is nothing to own - creating an empty
# Purchase would invent a service the customer never got.
check("no empty Purchase is invented", len(user.purchases), 0)

print("\n--- a second purchase stays separate from the first ---")
db, node, pkg = setup()
buy_new(db, node, pkg, "tg777")
user = db.query(models.User).filter_by(username="tg777").one()
pkg2 = models.Package(name="۱۰۰ گیگ ۲ ماه", price=200_000, quota_gb=100, duration_days=60)
db.add(pkg2)
db.commit()
db.refresh(pkg2)

user_ops.apply_package_as_purchase(
    db, user, pkg2,
    connections_override=[{"node_id": node.id, "protocol": "wireguard", "flow": ""}],
)
db.commit()
db.refresh(user)

check("two independent services now exist", len(user.purchases), 2)
check("still nothing on the shared pool",
      [c for c in user.connections if c.purchase_id is None], [])
names = sorted(p.package_name_snapshot for p in user.purchases)
check("each keeps its own package name", names, sorted(["۵۰ گیگ ۱ ماه", "۱۰۰ گیگ ۲ ماه"]))
check("their quotas add up rather than replace",
      user.effective_quota_bytes,
      sum(p.quota_bytes for p in user.purchases))
# The first purchase must NOT have been swallowed into the second.
first = [p for p in user.purchases if p.package_name_snapshot == "۵۰ گیگ ۱ ماه"][0]
check("the first service kept its own quota", first.quota_bytes, user_ops.gb_to_bytes(50))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
