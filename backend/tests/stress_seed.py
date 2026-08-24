"""Builds a throwaway database shaped like a 20,000-customer panel.

Run:  python3 backend/tests/stress_seed.py /tmp/stress.db

Not a test - the input for stress_bench.py. Deliberately writes to a file
you name, never to the panel's own database.

Shape (agreed with the panel owner 2026-08-24):
  20,000 customers, 3,000 of them online, roughly half RADIUS (PPP - so a
  live RadiusActiveSession row) and half Xray (Connection.online = True).

The proportions matter more than the exact numbers: an Xray "online" costs
a boolean already sitting on a row the list query loads anyway, while a
PPP "online" costs a join against a separate table. Getting that split
wrong would flatter or slander the list query for the wrong reason.
"""
from __future__ import annotations

import datetime as dt
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TARGET = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stress.db"
if os.path.exists(TARGET):
    os.unlink(TARGET)
os.environ["DATABASE_URL"] = f"sqlite:///{TARGET}"

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models

USERS = int(os.environ.get("STRESS_USERS", 20_000))
ONLINE = int(os.environ.get("STRESS_ONLINE", 3_000))
ADMINS = 30          # 1 superadmin + level-2 Admins + their Sellers
NODES = 12
PACKAGES = 25

random.seed(1404)
GB = 1024 ** 3
now = dt.datetime.utcnow()

engine = create_engine(f"sqlite:///{TARGET}")
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

t0 = time.perf_counter()

# ---- admins: a real 3-tier tree, since visibility filters walk it -------
su = models.AdminUser(username="su", hashed_password="x", is_superadmin=True, role="superadmin")
db.add(su)
db.flush()
su.tree_path, su.depth = f"/{su.id}/", 0

level2 = []
for i in range(6):
    a = models.AdminUser(username=f"admin{i}", hashed_password="x", role="admin",
                         parent_admin_id=su.id, balance=50_000_000)
    db.add(a)
    db.flush()
    a.tree_path, a.depth = f"/{su.id}/{a.id}/", 1
    level2.append(a)

sellers = []
for i in range(ADMINS - len(level2) - 1):
    parent = level2[i % len(level2)]
    s = models.AdminUser(username=f"seller{i}", hashed_password="x", role="seller",
                         parent_admin_id=parent.id, balance=5_000_000)
    db.add(s)
    db.flush()
    s.tree_path, s.depth = f"{parent.tree_path}{s.id}/", 2
    sellers.append(s)
db.commit()

owners = [su.id] + [a.id for a in level2] + [s.id for s in sellers]

# ---- nodes + packages ---------------------------------------------------
nodes = []
for i in range(NODES):
    n = models.Node(name=f"node{i}", type="mikrotik" if i % 2 else "xray", enabled=True,
                    mt_host=f"10.0.{i}.1", mt_username="api", mt_password="x")
    db.add(n)
    nodes.append(n)
db.commit()

packages = []
for i in range(PACKAGES):
    p = models.Package(name=f"پکیج {i}", quota_gb=random.choice([20, 50, 100, 200]),
                       duration_days=30, price=random.choice([90_000, 150_000, 250_000]),
                       cooperation_price=random.choice([40_000, 70_000, 120_000]), enabled=True)
    db.add(p)
    packages.append(p)
db.commit()

print(f"admins/nodes/packages ready in {time.perf_counter() - t0:.1f}s")

# ---- customers ----------------------------------------------------------
# Bulk inserts: the ORM one-object-at-a-time path would dominate the seed
# time and tell us nothing about the panel.
t1 = time.perf_counter()
user_rows = []
for i in range(USERS):
    quota = random.choice([20, 50, 100, 200]) * GB
    user_rows.append(dict(
        username=f"user{i:06d}",
        full_name=f"مشتری {i}",
        telegram_id=600_000_000 + i if i % 3 else None,
        owner_admin_id=random.choice(owners),
        package_id=random.choice(packages).id,
        total_quota_bytes=quota,
        used_bytes=random.randint(0, quota),
        balance=random.choice([0, 0, 0, 50_000, 200_000]),
        expire_at=now + dt.timedelta(days=random.randint(-30, 90)),
        status=models.UserStatus.active,
        created_via=random.choice(["bot", "panel", None]),
        created_at=now - dt.timedelta(days=random.randint(0, 700)),
        updated_at=now,
        purchases_blocked=(i % 500 == 0),
    ))
db.bulk_insert_mappings(models.User, user_rows)
db.commit()
print(f"{USERS:,} customers in {time.perf_counter() - t1:.1f}s")

user_ids = [r[0] for r in db.query(models.User.id).all()]

# ---- one Purchase + 1-3 Connections each --------------------------------
t2 = time.perf_counter()
purchase_rows = []
for uid in user_ids:
    quota = random.choice([20, 50, 100, 200]) * GB
    purchase_rows.append(dict(
        user_id=uid, package_id=random.choice(packages).id,
        quota_bytes=quota, used_bytes=random.randint(0, quota),
        expire_at=now + dt.timedelta(days=random.randint(-30, 90)),
        created_at=now - dt.timedelta(days=random.randint(0, 400)),
        updated_at=now,
        status=models.UserStatus.active,
    ))
db.bulk_insert_mappings(models.Purchase, purchase_rows)
db.commit()
purchase_ids = [r[0] for r in db.query(models.Purchase.id).all()]
print(f"{len(purchase_ids):,} purchases in {time.perf_counter() - t2:.1f}s")

t3 = time.perf_counter()
# Half the online users are Xray (Connection.online flag), half PPP (a live
# RadiusActiveSession row) - see this module's docstring for why the split
# is the point.
online_users = set(random.sample(user_ids, ONLINE))
xray_online = set(random.sample(sorted(online_users), ONLINE // 2))

conn_rows = []
for idx, (uid, pid) in enumerate(zip(user_ids, purchase_ids)):
    for k in range(random.choice([1, 1, 2, 3])):
        proto = random.choice(["openvpn", "l2tp", "xray", "wireguard"])
        conn_rows.append(dict(
            user_id=uid, purchase_id=pid, node_id=random.choice(nodes).id,
            type=proto,
            ppp_username=f"u{uid}_{k}", ppp_password="x",
            enabled=True,
            online=(uid in xray_online and k == 0),
            created_at=now,
        ))
db.bulk_insert_mappings(models.Connection, conn_rows)
db.commit()
print(f"{len(conn_rows):,} connections in {time.perf_counter() - t3:.1f}s")

# ---- live PPP sessions for the RADIUS half ------------------------------
t4 = time.perf_counter()
ppp_online = sorted(online_users - xray_online)
first_conn = {}
for cid, uid in db.query(models.Connection.id, models.Connection.user_id).filter(
        models.Connection.user_id.in_(ppp_online)).all():
    first_conn.setdefault(uid, cid)

session_rows = []
for uid, cid in first_conn.items():
    session_rows.append(dict(
        connection_id=cid,
        session_id=f"sess{uid}",
        nas_ip=f"10.0.{random.randint(0, 11)}.1",
        client_ip=f"172.16.{uid % 255}.{random.randint(2, 254)}",
        started_at=now - dt.timedelta(minutes=random.randint(1, 600)),
        last_seen_at=now - dt.timedelta(seconds=random.randint(0, 300)),
    ))
db.bulk_insert_mappings(models.RadiusActiveSession, session_rows)
db.commit()
print(f"{len(session_rows):,} live PPP sessions in {time.perf_counter() - t4:.1f}s")

# ---- ledger: a year of sales, which the accounting page reads -----------
t5 = time.perf_counter()
ledger_rows = []
for i in range(60_000):
    ledger_rows.append(dict(
        kind=random.choice(["sale_new", "sale_renew", "wallet_topup"]),
        amount=random.choice([90_000, 150_000, 250_000]),
        admin_id=random.choice(owners),
        user_id=random.choice(user_ids),
        created_at=now - dt.timedelta(days=random.randint(0, 365),
                                      seconds=random.randint(0, 86400)),
    ))
db.bulk_insert_mappings(models.LedgerEntry, ledger_rows)
db.commit()
print(f"{len(ledger_rows):,} ledger entries in {time.perf_counter() - t5:.1f}s")

size_mb = os.path.getsize(TARGET) / 1024 / 1024
print(f"\nDB: {TARGET}  ({size_mb:.0f} MB)  built in {time.perf_counter() - t0:.1f}s")
print(f"  customers={USERS:,}  online={ONLINE:,} (xray {len(xray_online):,} / ppp {len(session_rows):,})")
print(f"  connections={len(conn_rows):,}  purchases={len(purchase_ids):,}  ledger=60,000")
