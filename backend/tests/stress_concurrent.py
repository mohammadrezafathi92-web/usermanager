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
os.environ["DATABASE_URL"] = "sqlite:////tmp/stress.db"
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
    try:
        while not stop.is_set():
            t = time.perf_counter()
            try:
                users_router.list_users(page=(idx % 20) + 1, page_size=50, db=db,
                                        admin=db.query(models.AdminUser).filter_by(is_superadmin=True).first())
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
    db = SessionLocal()
    try:
        while not stop.is_set():
            t = time.perf_counter()
            users = db.query(models.User).options(
                selectinload(models.User.connections), selectinload(models.User.purchases)).all()
            for u in users:
                quota_manager._enforce_user_limits(db, u)
            db.commit()
            db.expunge_all()
            print(f"    [poll cycle: {time.perf_counter()-t:.1f}s]", flush=True)
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
