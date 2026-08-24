"""Admins browsing WHILE the poll loop and RADIUS writes are running.

Run:  python3 backend/tests/stress_seed.py /tmp/stress.db
      python3 backend/tests/stress_concurrent.py

Timing each thing on its own flatters the panel. Everything here shares one
SQLite file, so what matters is what happens when they run together - and
the answer turned out to be the opposite of the obvious guess: the pages
stay fast and the POLL LOOP is what stretches (3s alone, ~21s under load).

Uses the app's own engine on purpose, so WAL and busy_timeout are exactly
what production has. Writes only to the throwaway stress DB.
"""
import os, sys, time, threading, statistics, logging
sys.path.insert(0, "backend")
os.environ["DATABASE_URL"] = os.environ.get("STRESS_DB", "sqlite:////tmp/stress_settled.db")
logging.disable(logging.CRITICAL)
from sqlalchemy.orm import selectinload
from app.database import SessionLocal      # the app's engine: WAL + busy_timeout
from app import models
from app.routers import users as users_router
from app.services import quota_manager
import datetime as dt

stop = threading.Event()
latencies, errors = [], []

def reader(idx):
    db = SessionLocal()
    # The BUSIEST account, not the superadmin: hierarchy.owned_admin_ids
    # scopes a superadmin to only the customers they made themselves, so
    # browsing as one measures an almost empty table. Resolved once per
    # thread rather than re-queried on every iteration.
    from app.services import hierarchy as h
    admin = max(db.query(models.AdminUser).filter(models.AdminUser.role == "admin").all(),
                key=lambda a: db.query(models.User).filter(
                    models.User.owner_admin_id.in_(h.owned_admin_ids(db, a))).count())
    try:
        while not stop.is_set():
            t = time.perf_counter()
            try:
                users_router.list_users(page=(idx % 20) + 1, page_size=50, db=db, admin=admin)
                latencies.append((time.perf_counter() - t) * 1000)
            except Exception as e:
                errors.append(f"read: {type(e).__name__}")
            db.expunge_all()
    finally:
        db.close()

def radius_writer():
    """1,500 sessions checking in - the steady accounting drumbeat."""
    db = SessionLocal()
    n = 0
    try:
        while not stop.is_set():
            s = db.query(models.RadiusActiveSession).filter_by(id=(n % 1500) + 1).first()
            if s:
                s.last_seen_at = dt.datetime.utcnow()
                db.commit()
            n += 1
            time.sleep(0.04)   # 25 packets/sec = interim-update 1 min
    except Exception as e:
        errors.append(f"radius: {type(e).__name__}: {e}")
    finally:
        db.close()

def poller():
    """poll_all's enforcement half, exactly as it now runs in production."""
    db = SessionLocal()
    try:
        while not stop.is_set():
            t = time.perf_counter()
            now = dt.datetime.utcnow()
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
            db.commit()
            db.expunge_all()
            print(f"    [poll cycle: {time.perf_counter()-t:.2f}s  "
                  f"{len(pids)} services + {len(uids)} accounts]", flush=True)
            time.sleep(1)
    except Exception as e:
        errors.append(f"poll: {type(e).__name__}: {e}")
    finally:
        db.close()

print("6 admins browsing + 25 RADIUS writes/sec + the poll loop, for 20 seconds:\n")
threads = [threading.Thread(target=reader, args=(i,), daemon=True) for i in range(6)]
threads += [threading.Thread(target=radius_writer, daemon=True),
            threading.Thread(target=poller, daemon=True)]
for t in threads: t.start()
time.sleep(20)
stop.set()
for t in threads: t.join(timeout=15)

lat = sorted(latencies)
print(f"\n  page loads completed   {len(lat):,}  ({len(lat)/20:.0f}/sec)")
print(f"  median                 {statistics.median(lat):6.1f} ms")
print(f"  p95                    {lat[int(len(lat)*.95)]:6.1f} ms")
print(f"  p99                    {lat[int(len(lat)*.99)]:6.1f} ms")
print(f"  worst                  {lat[-1]:6.1f} ms")
print(f"  errors                 {len(errors)}" + (f"  {errors[:3]}" if errors else "  (none)"))
