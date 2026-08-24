"""The optimised poll loop must decide EXACTLY what the old one decided.

Run:  python3 backend/tests/test_poll_equivalence.py

quota_manager.poll_all used to load every user, purchase and connection on
the panel and run the enforcement functions over all of them. It now runs
the same rules over cheap tuples first and only loads the rows that can
change. That is only a safe trade if the two select the same rows - a
missed row means a customer who is out of quota keeps browsing, or one who
renewed stays cut off.

So this does not test that the new code is fast. It tests that it is the
same. Both selections run over identical databases and every user's and
every purchase's resulting status is compared, row by row.

The cases are built to include the ones a filter is most likely to get
wrong: a queued renewal sitting behind an already-exhausted service (the
status is not changing, but something still must happen), a manually
disabled row (must be left alone), and a customer with legacy connections
(judged by different rules entirely).
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
from app.services import quota_manager

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
    """One database containing every interesting shape at once."""
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    admin = models.AdminUser(username="su", hashed_password="x", is_superadmin=True)
    node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
    pkg = models.Package(name="p", quota_gb=50, price=1)
    db.add_all([admin, node, pkg])
    db.commit()

    def customer(name, status=models.UserStatus.active, **user_kw):
        user_kw.setdefault("total_quota_bytes", 0)
        user_kw.setdefault("used_bytes", 0)
        u = models.User(username=name, status=status, owner_admin_id=admin.id, **user_kw)
        db.add(u)
        db.flush()
        return u

    def service(user, *, quota, used, days, status=models.UserStatus.active,
                res_quota=None, res_days=None, legacy=False):
        p = models.Purchase(
            user_id=user.id, package_id=pkg.id, status=status,
            quota_bytes=quota, used_bytes=used,
            expire_at=(NOW + dt.timedelta(days=days)) if days is not None else None,
            reserved_quota_bytes=res_quota, reserved_duration_days=res_days,
        )
        db.add(p)
        db.flush()
        c = models.Connection(user_id=user.id, node_id=node.id, type="l2tp",
                              purchase_id=None if legacy else p.id,
                              ppp_username=f"{user.username}_c", ppp_password="x",
                              enabled=True)
        db.add(c)
        db.flush()
        return p

    # --- healthy: nothing should happen -----------------------------------
    for i in range(20):
        u = customer(f"ok{i}")
        service(u, quota=50 * GB, used=10 * GB, days=30)

    # --- newly over quota: must be caught ---------------------------------
    for i in range(5):
        u = customer(f"overquota{i}")
        service(u, quota=50 * GB, used=60 * GB, days=30)

    # --- newly expired ----------------------------------------------------
    for i in range(5):
        u = customer(f"expired{i}")
        service(u, quota=50 * GB, used=1 * GB, days=-1)

    # --- already marked exhausted and STILL exhausted: no-op --------------
    u = customer("settled", status=models.UserStatus.quota_exceeded)
    service(u, quota=50 * GB, used=60 * GB, days=30, status=models.UserStatus.quota_exceeded)

    # --- the trap: exhausted, already marked so, but a renewal is QUEUED --
    # The status is not changing, so a naive "status != target" filter drops
    # this row - and the customer's paid renewal never activates.
    u = customer("queued_renewal", status=models.UserStatus.quota_exceeded)
    service(u, quota=50 * GB, used=60 * GB, days=30,
            status=models.UserStatus.quota_exceeded, res_quota=100 * GB, res_days=30)

    u = customer("queued_after_expiry", status=models.UserStatus.expired)
    service(u, quota=50 * GB, used=1 * GB, days=-5,
            status=models.UserStatus.expired, res_days=30)

    # --- manually disabled: must be left completely alone ------------------
    u = customer("disabled_user", status=models.UserStatus.disabled)
    service(u, quota=50 * GB, used=99 * GB, days=-9)

    u = customer("disabled_service")
    service(u, quota=50 * GB, used=99 * GB, days=-9, status=models.UserStatus.disabled)

    # --- legacy pool customers: judged by their OWN fields -----------------
    u = customer("legacy_over", total_quota_bytes=50 * GB, used_bytes=60 * GB)
    service(u, quota=0, used=0, days=None, legacy=True)

    u = customer("legacy_fine", total_quota_bytes=50 * GB, used_bytes=1 * GB,
                 expire_at=NOW + dt.timedelta(days=30))
    service(u, quota=0, used=0, days=None, legacy=True)

    # --- unlimited / no expiry: never exhausted ---------------------------
    u = customer("unlimited")
    service(u, quota=0, used=999 * GB, days=None)

    # --- account whose only service is exhausted: badge must follow -------
    u = customer("badge_follows")
    service(u, quota=50 * GB, used=60 * GB, days=30)

    db.commit()
    return db


def snapshot(db):
    """Every status in the database, as plain comparable data."""
    users = dict(db.query(models.User.username, models.User.status).all())
    purchases = {
        pid: (status, quota, used, expire is not None, res_q, res_d)
        for pid, status, quota, used, expire, res_q, res_d in db.query(
            models.Purchase.id, models.Purchase.status, models.Purchase.quota_bytes,
            models.Purchase.used_bytes, models.Purchase.expire_at,
            models.Purchase.reserved_quota_bytes, models.Purchase.reserved_duration_days,
        ).all()
    }
    conns = dict(db.query(models.Connection.id, models.Connection.enabled).all())
    return users, purchases, conns


def old_enforcement(db):
    """poll_all's enforcement half exactly as it was before the change."""
    users = db.query(models.User).options(
        selectinload(models.User.connections),
        selectinload(models.User.purchases),
    ).all()
    for user in users:
        quota_manager._enforce_user_limits(db, user)
    purchases = db.query(models.Purchase).options(
        selectinload(models.Purchase.connections)).all()
    for purchase in purchases:
        quota_manager._enforce_purchase_limits(db, purchase)
    db.commit()


def new_enforcement(db):
    """...and exactly as it is now."""
    now = dt.datetime.utcnow()
    purchase_ids = quota_manager._purchase_ids_needing_enforcement(db, now)
    if purchase_ids:
        for p in db.query(models.Purchase).options(
                selectinload(models.Purchase.connections)).filter(
                models.Purchase.id.in_(purchase_ids)).all():
            quota_manager._enforce_purchase_limits(db, p)
        db.flush()
    user_ids = quota_manager._user_ids_needing_enforcement(db, now)
    if user_ids:
        for u in db.query(models.User).options(
                selectinload(models.User.connections),
                selectinload(models.User.purchases)).filter(
                models.User.id.in_(user_ids)).all():
            quota_manager._enforce_user_limits(db, u)
    db.commit()
    return purchase_ids, user_ids


print("--- where the two settle: identical ---")
# The old loop enforced ACCOUNTS before SERVICES, so an account's badge was
# derived from the previous cycle's service statuses - always one cycle
# behind. The new loop does services first, so it lands on the right answer
# immediately. That is the one deliberate behavioural difference, and it
# only shows up in the first cycle; both settle in the same place, which is
# what actually has to hold.
db_old, db_new = build(), build()
for _ in range(3):
    old_enforcement(db_old)
pids, uids = new_enforcement(db_new)

u_old, p_old, c_old = snapshot(db_old)
u_new, p_new, c_new = snapshot(db_new)

check("every account ends in the same status", u_new, u_old)
check("every service ends in the same state", p_new, p_old)
check("every connection ends equally enabled", c_new, c_old)

print("\n--- and the new one gets there SOONER ---")
db_slow, db_fast = build(), build()
old_enforcement(db_slow)          # one cycle
new_enforcement(db_fast)          # one cycle
lagging = {k for k in u_old if snapshot(db_slow)[0][k] != snapshot(db_fast)[0][k]}
check("after ONE cycle the new code is already settled",
      snapshot(db_fast)[0], u_old)
print(f"  {len(lagging)} account(s) the old code still had wrong after one cycle: "
      f"{sorted(lagging) if lagging else 'none'}")
old_enforcement(db_slow)          # second cycle
check("the old code needed a SECOND cycle to catch up",
      snapshot(db_slow)[0], u_old)

print(f"\n  the new pass loaded {len(pids)} service(s) and {len(uids)} account(s)")
total_p = db_new.query(models.Purchase).count()
total_u = db_new.query(models.User).count()
print(f"  instead of all {total_p} and all {total_u}")
check("it really did skip most of them", len(pids) < total_p, True)

print("\n--- the specific traps, checked by name ---")
db2 = build()
new_enforcement(db2)

got = dict(db2.query(models.User.username, models.User.status).all())
check("a queued renewal behind an exhausted service DID activate",
      db2.query(models.Purchase).join(models.User).filter(
          models.User.username == "queued_renewal").first().reserved_quota_bytes, None)
check("...and its quota was raised",
      db2.query(models.Purchase).join(models.User).filter(
          models.User.username == "queued_renewal").first().quota_bytes, 100 * GB)
check("...and its usage reset",
      db2.query(models.Purchase).join(models.User).filter(
          models.User.username == "queued_renewal").first().used_bytes, 0)
check("a queued renewal behind an EXPIRED service also activated",
      db2.query(models.Purchase).join(models.User).filter(
          models.User.username == "queued_after_expiry").first().reserved_duration_days, None)
check("a manually disabled account was not touched",
      got["disabled_user"], models.UserStatus.disabled)
check("a manually disabled service was not touched",
      db2.query(models.Purchase).join(models.User).filter(
          models.User.username == "disabled_service").first().status,
      models.UserStatus.disabled)
check("a legacy-pool customer over quota was caught",
      got["legacy_over"], models.UserStatus.quota_exceeded)
check("a healthy legacy-pool customer was left active",
      got["legacy_fine"], models.UserStatus.active)
check("an unlimited service never expires", got["unlimited"], models.UserStatus.active)
check("an account follows its exhausted service",
      got["badge_follows"], models.UserStatus.quota_exceeded)
check("a healthy account stays active", got["ok0"], models.UserStatus.active)
check("a newly over-quota account was caught",
      got["overquota0"], models.UserStatus.quota_exceeded)
check("a newly expired account was caught",
      got["expired0"], models.UserStatus.expired)

print("\n--- and it must be STABLE: a second cycle changes nothing more ---")
before = snapshot(db2)
pids2, uids2 = new_enforcement(db2)
check("a second pass leaves everything identical", snapshot(db2), before)
check("...and finds no service left to touch", len(pids2), 0)
# Customers with LEGACY connections are always re-examined: their status
# comes from their own combined quota/expiry, which a status comparison
# cannot rule out cheaply. There are only a handful left on this install,
# so they are simply always included rather than special-cased further.
n_legacy = db2.query(models.Connection.user_id).filter(
    models.Connection.purchase_id.is_(None)).distinct().count()
check("...and only re-examines the legacy-pool customers", len(uids2), n_legacy)

print("\n--- repeated cycles still agree with the old code ---")
db_old2, db_new2 = build(), build()
for _ in range(3):
    old_enforcement(db_old2)
    new_enforcement(db_new2)
check("after three cycles, still identical", snapshot(db_new2), snapshot(db_old2))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("کد جدید مو‌به‌مو همان تصمیم کد قدیم را می‌گیرد")
