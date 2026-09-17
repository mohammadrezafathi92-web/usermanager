"""Usage is counted per SESSION, not per connection.

Run:  python3 backend/tests/test_radius_usage_per_session.py

Reported 2026-09-17 by a reseller running a customer's panel: the panel
counts far more volume than their users actually use.

RADIUS accounting counters are per session - each one starts at zero and
grows - but the baseline was kept per CONNECTION
(Connection.radius_session_id + last_rx_bytes). With one session those are
the same thing. With two, which is exactly what a «۳ کاربر همزمان» package
is sold to allow, each report found a baseline belonging to the OTHER
session, concluded the counter had reset, and added that session's entire
cumulative total again.

Reproduced before changing anything: two devices, 1.4 GB really used, 3.6
GB recorded - and it compounds the longer the sessions stay up, because
what gets re-added each time is the running total, not the increment.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import quota_manager as qm
from app.services.radius_server import UserManagerRadiusServer as RadiusServer

failures: list[str] = []
GB = 1024 ** 3


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def close(label, got_bytes, expected_gb, tolerance_gb=0.001):
    got_gb = got_bytes / GB
    if abs(got_gb - expected_gb) <= tolerance_gb:
        print(f"PASS  {label}  ({got_gb:.2f} GB)")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got_gb:.2f} GB\n        expected: {expected_gb:.2f} GB")


def make():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.Node(id=1, name="n", type=models.NodeType.mikrotik,
                       mt_host="1.2.3.4", mt_username="u", mt_password="p"))
    db.add(models.User(id=1, username="cust", max_concurrent_sessions=3))
    db.commit()
    conn = models.Connection(id=1, user_id=1, node_id=1, type=models.ConnectionType.l2tp,
                             ppp_username="cust", max_concurrent_sessions=3)
    db.add(conn)
    db.commit()
    return db, conn


def report(db, conn, session_id: str, cumulative_gb: float):
    """One Interim-Update, the way HandleAcctPacket now handles it."""
    delta = RadiusServer._session_delta(db, conn.id, session_id, int(cumulative_gb * GB), 0)
    qm.add_usage(db, conn, delta)
    db.commit()


print("--- two devices on one account, both online ---")
db, conn = make()
# Each row is that session's own running total, exactly as a NAS reports it.
for sid, total in [("A", 1.0), ("B", 0.1), ("A", 1.1), ("B", 0.2), ("A", 1.2)]:
    report(db, conn, sid, total)

user = db.get(models.User, 1)
# A ended at 1.2, B at 0.2.
close("the recorded total matches what they really used", user.used_bytes, 1.4)
close("...and so does the connection's own counter", conn.total_bytes, 1.4)

print("\n--- one device, the simple case, is unchanged ---")
db, conn = make()
for total in (0.5, 1.0, 2.5):
    report(db, conn, "solo", total)
close("a single session still counts once", db.get(models.User, 1).used_bytes, 2.5)

print("\n--- ten devices, which the panel sells ---")
db, conn = make()
for step in range(1, 6):
    for n in range(10):
        report(db, conn, f"s{n}", step * 0.1)
# Ten sessions that each ended at 0.5 GB.
close("all ten add up to exactly their totals", db.get(models.User, 1).used_bytes, 5.0)

print("\n--- a session that reconnects under the same id ---")
db, conn = make()
report(db, conn, "X", 2.0)
report(db, conn, "X", 0.3)   # NAS restarted the session; counter went back
report(db, conn, "X", 0.8)
close("the restart is treated as a fresh count, not a negative",
      db.get(models.User, 1).used_bytes, 2.8)

print("\n--- a Stop packet is counted before the row is deleted ---")
db, conn = make()
report(db, conn, "Y", 1.0)
delta = RadiusServer._session_delta(db, conn.id, "Y", int(3.0 * GB), 0)
qm.add_usage(db, conn, delta)
RadiusServer._close_active_session(db, conn.id, "Y")
db.commit()
close("the final report is not lost", db.get(models.User, 1).used_bytes, 3.0)
check("...and the session row is gone",
      db.query(models.RadiusActiveSession).count(), 0)

print("\n--- the pollers are untouched ---")
# WireGuard and Xray report ONE cumulative counter per peer, so the
# per-connection baseline is correct for them and _apply_delta keeps it.
db, conn = make()
qm._apply_delta(db, conn, int(1.0 * GB), 0)
qm._apply_delta(db, conn, int(1.5 * GB), 0)
db.commit()
close("a poller's cumulative counter still deltas per connection",
      db.get(models.User, 1).used_bytes, 1.5)
qm._apply_delta(db, conn, int(0.2 * GB), 0)   # peer recreated
db.commit()
close("...and a recreated peer restarts the count",
      db.get(models.User, 1).used_bytes, 1.7)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("حجم دقیقاً همان‌قدر شمرده می‌شود که مصرف شده")
