"""Times the panel's hot paths against the 20,000-customer database.

Run:  python3 backend/tests/stress_seed.py /tmp/stress.db
      python3 backend/tests/stress_bench.py /tmp/stress.db

Calls the REAL router functions, not hand-written SQL that resembles them.
A benchmark of a query I retyped would measure my retyping.

Each figure is the median of several runs plus the worst one, because a
panel that is usually fast and occasionally unusable is experienced as
slow - the tail is the part an admin complains about.
"""
from __future__ import annotations

import datetime as dt
import os
import statistics
import sys
import time
import tracemalloc

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DB = sys.argv[1] if len(sys.argv) > 1 else "/tmp/stress.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app import models
from app.routers import users as users_router
from app.routers import dashboard as dashboard_router
from app.services import accounting, hierarchy

engine = create_engine(f"sqlite:///{DB}")
Session = sessionmaker(bind=engine)

# Count the SQL statements each call makes. Wall time on one machine ages
# badly; "this endpoint issues 1 query or 200" does not, and N+1 is the
# failure mode that actually bites as a table grows.
_counter = {"n": 0}


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    _counter["n"] += 1


def bench(label, fn, runs=5, budget_ms=None):
    times, queries = [], 0
    for i in range(runs):
        _counter["n"] = 0
        t = time.perf_counter()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - a failure here is a result
            print(f"  {label:<44} ERROR: {type(exc).__name__}: {exc}")
            return None
        times.append((time.perf_counter() - t) * 1000)
        queries = _counter["n"]
    med, worst = statistics.median(times), max(times)
    verdict = ""
    if budget_ms:
        verdict = "  ✓" if med <= budget_ms else f"  ← SLOW (>{budget_ms}ms)"
    print(f"  {label:<44} {med:7.1f} ms   worst {worst:7.1f}   {queries:>4} queries{verdict}")
    return med


db = Session()
su = db.query(models.AdminUser).filter_by(is_superadmin=True).first()
lvl2 = db.query(models.AdminUser).filter_by(role="admin").first()
seller = db.query(models.AdminUser).filter_by(role="seller").first()

n_users = db.query(models.User).count()
n_conns = db.query(models.Connection).count()
n_sess = db.query(models.RadiusActiveSession).count()
print(f"database: {n_users:,} customers, {n_conns:,} connections, {n_sess:,} live PPP sessions")
print(f"file size: {os.path.getsize(DB) / 1024 / 1024:.0f} MB\n")

print("=" * 96)
print("PANEL PAGES - what an admin waits for")
print("=" * 96)

bench("users list, page 1 (superadmin)",
      lambda: users_router.list_users(page=1, page_size=50, db=db, admin=su), budget_ms=300)
bench("users list, page 200 (deep paging)",
      lambda: users_router.list_users(page=200, page_size=50, db=db, admin=su), budget_ms=300)
bench("users list, page 1 (level-2 admin, scoped)",
      lambda: users_router.list_users(page=1, page_size=50, db=db, admin=lvl2), budget_ms=300)
bench("users list, page 1 (seller, scoped)",
      lambda: users_router.list_users(page=1, page_size=50, db=db, admin=seller), budget_ms=300)
bench("users list, search 'user12'",
      lambda: users_router.list_users(page=1, page_size=50, search="user12", db=db, admin=su), budget_ms=400)
bench("users list, ONLINE ONLY filter",
      lambda: users_router.list_users(page=1, page_size=50, online_only=True, db=db, admin=su), budget_ms=400)
bench("users list, page_size=200",
      lambda: users_router.list_users(page=1, page_size=200, db=db, admin=su), budget_ms=600)

print()
bench("dashboard stats (superadmin)",
      lambda: dashboard_router.stats(db=db, admin=su), budget_ms=500)
bench("dashboard stats (level-2 admin)",
      lambda: dashboard_router.stats(db=db, admin=lvl2), budget_ms=500)

print()
bench("accounting summary, all time (superadmin)",
      lambda: accounting.summary(db, su), budget_ms=800)
bench("accounting summary, last 30 days",
      lambda: accounting.summary(db, su, date_from=dt.datetime.utcnow() - dt.timedelta(days=30)),
      budget_ms=500)
bench("accounting summary (level-2 admin)",
      lambda: accounting.summary(db, lvl2), budget_ms=500)
bench("accounting subtree rollup",
      lambda: accounting.subtree_rollup(db, su), budget_ms=800)
bench("accounting series (chart)",
      lambda: accounting.series(db, su), budget_ms=800)

print()
bench("hierarchy: subtree_ids (superadmin)",
      lambda: hierarchy.subtree_ids(db, su), budget_ms=50)
bench("hierarchy: owned_admin_ids (level-2)",
      lambda: hierarchy.owned_admin_ids(db, lvl2), budget_ms=50)

print()
print("=" * 96)
print("BULK / EXPORT - the things that hold a worker busy")
print("=" * 96)
bench("user ids for 'select all matching filter'",
      lambda: users_router.list_user_ids(db=db, admin=su), runs=3, budget_ms=1000)

print()
print("=" * 96)
print("MEMORY - one request's worth")
print("=" * 96)
for label, fn in [
    ("users list page (50 rows)", lambda: users_router.list_users(page=1, page_size=50, db=db, admin=su)),
    ("users list page (200 rows)", lambda: users_router.list_users(page=1, page_size=200, db=db, admin=su)),
    ("accounting summary", lambda: accounting.summary(db, su)),
    ("select-all id list", lambda: users_router.list_user_ids(db=db, admin=su)),
]:
    db.expunge_all()
    tracemalloc.start()
    try:
        fn()
        _, peak = tracemalloc.get_traced_memory()
        print(f"  {label:<44} peak {peak / 1024 / 1024:6.1f} MB")
    except Exception as exc:  # noqa: BLE001
        print(f"  {label:<44} ERROR: {exc}")
    finally:
        tracemalloc.stop()

print()
print("=" * 96)
print("INDEX CHECK - what the planner actually does")
print("=" * 96)
raw = engine.raw_connection()
cur = raw.cursor()
plans = [
    ("users list ordering", "SELECT id FROM users ORDER BY id DESC LIMIT 50"),
    ("users by owner", "SELECT id FROM users WHERE owner_admin_id = 3 LIMIT 50"),
    ("username search", "SELECT id FROM users WHERE username LIKE '%user12%' LIMIT 50"),
    ("online join", "SELECT c.user_id FROM connections c JOIN radius_active_sessions s ON s.connection_id = c.id"),
    ("ledger by date", "SELECT SUM(amount) FROM ledger_entries WHERE created_at >= '2026-01-01'"),
    ("ledger by admin", "SELECT SUM(amount) FROM ledger_entries WHERE admin_id = 3"),
    ("connections by user", "SELECT id FROM connections WHERE user_id = 500"),
]
for label, sql in plans:
    plan = " | ".join(str(r[-1]) for r in cur.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall())
    scan = "SCAN" in plan and "USING INDEX" not in plan and "USING COVERING INDEX" not in plan
    print(f"  {label:<24} {'FULL SCAN ←' if scan else 'indexed   '}  {plan[:88]}")
raw.close()
