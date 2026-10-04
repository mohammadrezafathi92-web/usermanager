"""A RADIUS Stop packet must COMMIT: its final usage is counted and the
session row goes away.

Run:  python3 backend/tests/test_radius_stop_commit.py

Seen on a production log, 2026-10-04, on every Stop of a session whose
counters had moved:

    StaleDataError: UPDATE statement on table 'radius_active_sessions'
    expected to update 1 row(s); 0 were matched.

The handler updates the session row's baseline (_session_delta) and then
closes the session. Closing was a bulk DELETE, which the session does not
know about; the application's sessions run with autoflush OFF
(database.SessionLocal), so the baseline UPDATE was still pending at commit
and hit a row that was already gone. The whole transaction rolled back -
the last usage report of the session (usually the largest) was never
counted, and the row stayed behind as a phantom "online" session.

test_radius_usage_per_session.py did not catch it: it uses a default
(autoflush ON) session, where the UPDATE happens to be flushed first.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.database import SessionLocal
from app.services.radius_server import UserManagerRadiusServer as RadiusServer

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


check("the application's sessions really run with autoflush off", SessionLocal.kw.get("autoflush"), False)

engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()     # same settings as SessionLocal
S = models.RadiusActiveSession


def rows():
    db.expire_all()
    return [(r.session_id, r.last_rx_bytes, r.last_tx_bytes) for r in db.query(S).order_by(S.id)]


def stop(session_id, in_octets, out_octets):
    """What HandleAcctPacket does for a Stop, in the same order."""
    try:
        delta = RadiusServer._session_delta(db, 1, session_id, in_octets, out_octets)
        RadiusServer._close_active_session(db, 1, session_id)
        db.commit()
        return delta
    except Exception as exc:
        db.rollback()
        return type(exc).__name__


print("--- a session we saw start, whose counters moved ---")
db.add(S(connection_id=1, session_id="a", last_rx_bytes=100, last_tx_bytes=200))
db.add(S(connection_id=1, session_id="other", last_rx_bytes=1, last_tx_bytes=1))
db.commit()
check("the Stop commits and returns the final delta (download, upload)", stop("a", 150, 500), (300, 50))
check("its row is gone; another session of the same connection is untouched", rows(), [("other", 1, 1)])

print("--- a session whose counters did not move ---")
check("commits too", (stop("other", 1, 1), rows()), ((0, 0), []))

print("--- a Stop for a session we never saw start ---")
check("its whole total is counted, and no phantom row is left behind", (stop("never-seen", 10, 20), rows()), ((20, 10), []))

print("--- interim then stop, same transaction boundaries as production ---")
db.add(S(connection_id=1, session_id="b"))
db.commit()
RadiusServer._session_delta(db, 1, "b", 5, 5)
db.commit()
check("stop after an interim update", (stop("b", 9, 6), rows()), ((1, 4), []))

db.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
