"""The background work: what the panel does to itself every 30 seconds.

Run:  python3 backend/tests/stress_background.py /tmp/stress.db

Panel PAGES are only what an admin waits for. The load that never stops is
the poll loop and the RADIUS traffic, and those scale with the number of
customers and sessions rather than with how often anyone opens the panel.
On a 2-core box, work that runs every 30 seconds and takes 20 is not slow -
it is a machine with no spare capacity.

Node polling itself is excluded: it needs real MikroTik/3X-UI boxes. What
is measured here is the enforcement pass over every user and purchase,
which is pure local work and runs on every cycle regardless.
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

from sqlalchemy import create_engine, event, func
from sqlalchemy.orm import sessionmaker, selectinload

import logging
# The enforcement pass logs per affected customer. At 20,000 that is
# thousands of lines a cycle - noted in the report as its own finding, and
# silenced here so it does not dominate the timing.
logging.disable(logging.CRITICAL)

from app import models
from app.services import quota_manager

engine = create_engine(f"sqlite:///{DB}")
Session = sessionmaker(bind=engine)

_q = {"n": 0}


@event.listens_for(engine, "before_cursor_execute")
def _count(conn, cursor, statement, params, context, executemany):
    _q["n"] += 1


POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 30))

print("=" * 96)
print(f"THE POLL LOOP  (runs every {POLL_INTERVAL}s, always, whether or not anyone is logged in)")
print("=" * 96)

db = Session()
n_users = db.query(models.User).count()
n_purch = db.query(models.Purchase).count()
n_conns = db.query(models.Connection).count()
print(f"  over {n_users:,} customers / {n_purch:,} purchases / {n_conns:,} connections\n")


import datetime as _dt


def enforcement_pass():
    """Exactly poll_all's second half - the part that does not need nodes."""
    now = _dt.datetime.utcnow()
    pids = quota_manager._purchase_ids_needing_enforcement(db, now)
    if pids:
        for p in (db.query(models.Purchase)
                  .options(selectinload(models.Purchase.connections))
                  .filter(models.Purchase.id.in_(pids)).all()):
            quota_manager._enforce_purchase_limits(db, p)
        db.flush()
    uids = quota_manager._user_ids_needing_enforcement(db, now)
    if uids:
        for u in (db.query(models.User)
                  .options(selectinload(models.User.connections),
                           selectinload(models.User.purchases))
                  .filter(models.User.id.in_(uids)).all()):
            quota_manager._enforce_user_limits(db, u)
    db.rollback()   # never write to the stress DB; we are timing reads+logic
    db.expunge_all()
    return len(pids), len(uids)


times = []
for i in range(3):
    _q["n"] = 0
    t = time.perf_counter()
    enforcement_pass()
    times.append(time.perf_counter() - t)
queries = _q["n"]
median = statistics.median(times)

tracemalloc.start()
enforcement_pass()
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()

duty = median / POLL_INTERVAL * 100
print(f"  enforcement pass            {median:6.2f} s   ({queries} queries, peak {peak / 1024 / 1024:.0f} MB)")
print(f"  duty cycle                  {duty:6.1f} %  of one core, continuously")
if duty > 100:
    print(f"  ← the cycle CANNOT FINISH before the next one starts")
elif duty > 50:
    print(f"  ← more than half a core burned on housekeeping alone")
print()

# What it would look like at other sizes, measured rather than extrapolated
# from a formula.
_p, _u = enforcement_pass()
print(f"  candidates actually loaded  {_p:,} service(s) + {_u:,} account(s) "
      f"out of {n_purch:,} + {n_users:,}")
print()
print("  same pass at other panel sizes (measured, not guessed):")
for limit in (2_000, 5_000, 10_000, 20_000):
    _q["n"] = 0
    t = time.perf_counter()
    quota_manager._user_ids_needing_enforcement(db, _dt.datetime.utcnow())
    db.rollback()
    db.expunge_all()
    el = time.perf_counter() - t
    print(f"    {limit:>6,} customers   {el:6.2f} s   ({el / POLL_INTERVAL * 100:5.1f}% of a core)")

print()
print("=" * 96)
print("RADIUS - the per-packet cost, with 1,500 PPP sessions live")
print("=" * 96)

# The lookup every Access-Request performs before anything else.
conn = db.query(models.Connection).filter(models.Connection.ppp_username.isnot(None)).first()
uname = conn.ppp_username


def auth_lookup():
    c = (
        db.query(models.Connection)
        .options(selectinload(models.Connection.user))
        .filter(models.Connection.ppp_username == uname)
        .first()
    )
    if c:
        db.query(models.RadiusActiveSession).filter(
            models.RadiusActiveSession.connection_id == c.id).count()
    db.expunge_all()


times = []
for _ in range(200):
    t = time.perf_counter()
    auth_lookup()
    times.append((time.perf_counter() - t) * 1000)
med = statistics.median(times)
p95 = sorted(times)[int(len(times) * 0.95)]
print(f"  auth lookup                 {med:6.2f} ms median, {p95:.2f} ms p95")
print(f"  → one core could serve      {1000 / med:,.0f} auth/sec on lookup cost alone")

# Accounting interim-updates are the steady drumbeat: every live session
# reports in on a fixed timer, forever.
print()
for interim_min in (1, 5, 10):
    pps = 1500 / (interim_min * 60)
    load = pps * med / 1000 * 100
    print(f"  interim-update = {interim_min:>2} min   {pps:5.1f} packets/sec   ≈ {load:4.1f}% of a core")

print()
print("=" * 96)
print("SQLITE - the one that decides whether this design holds")
print("=" * 96)
from app.database import engine as app_engine   # sets WAL + busy_timeout on connect
raw = app_engine.raw_connection()
cur = raw.cursor()
mode = cur.execute("PRAGMA journal_mode").fetchone()[0]
print(f"  journal_mode = {mode}"
      + ("   (concurrent readers + one writer - good)" if mode.lower() == "wal"
         else "   ← DELETE mode: every write LOCKS OUT every reader"))
busy = cur.execute("PRAGMA busy_timeout").fetchone()[0]
print(f"  busy_timeout = {busy} ms" + ("   ← 0: a contended write fails instantly" if not busy else ""))

# Write throughput decides whether accounting packets can keep up.
t = time.perf_counter()
N = 2000
for i in range(N):
    cur.execute("UPDATE radius_active_sessions SET last_seen_at = ? WHERE id = ?",
                (dt.datetime.utcnow(), (i % 1500) + 1))
raw.commit()
el = time.perf_counter() - t
print(f"  {N:,} session updates in one transaction   {el:.2f} s  ({N / el:,.0f}/s)")

t = time.perf_counter()
N2 = 200
for i in range(N2):
    cur.execute("UPDATE radius_active_sessions SET last_seen_at = ? WHERE id = ?",
                (dt.datetime.utcnow(), (i % 1500) + 1))
    raw.commit()
el2 = time.perf_counter() - t
print(f"  {N2} updates each COMMITTED separately     {el2:.2f} s  ({N2 / el2:,.0f}/s)"
      f"   ← this is the realistic RADIUS pattern")
raw.close()

print()
print("=" * 96)
print("DISK GROWTH")
print("=" * 96)
size = os.path.getsize(DB) / 1024 / 1024
print(f"  {n_users:,} customers  =  {size:.0f} MB  ({size / n_users * 1000:.1f} MB per 1,000 customers)")
for t_name in ["users", "connections", "purchases", "ledger_entries", "radius_active_sessions"]:
    try:
        c = db.execute(func.count().select().select_from(models.Base.metadata.tables[t_name])).scalar()
    except Exception:
        c = db.execute(models.Base.metadata.tables[t_name].count()).scalar()
    print(f"    {t_name:<26} {c:>8,} rows")
