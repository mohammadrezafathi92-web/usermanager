"""A single-session account with both a PPP service (OpenVPN/L2TP) and a
WireGuard/Xray one could have BOTH online at once, even with
max_concurrent_sessions=1 - reported 2026-09-06.

Run:  python3 backend/tests/test_concurrent_session_kick.py

Why: OpenVPN/L2TP logins go through this panel's own RADIUS server, so a
login that would exceed the cap is simply refused live (see
radius_server.py). WireGuard and Xray have no such login event - a peer/
client that already exists on the node just works the moment its keys are
used, with nothing asking the panel for permission. So a customer already
connected via OpenVPN could still open WireGuard too, with nothing to stop
them at the moment they do it.

services.quota_manager.enforce_concurrent_session_limits is the reactive
fix: called every poll cycle (see poll_all), it kicks (force-disables) the
excess non-PPP connection, and releases it back once there is genuinely
room again - without flapping it on and off (see its own docstring for
exactly why a naive version would).

This test monkeypatches _set_connection_enabled to a network-free stub
(records calls, flips connection.enabled) - the real function's MikroTik/
Xray API calls are exercised elsewhere; this file is only about the
kick/release DECISION logic.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import quota_manager

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


calls: list[tuple[int, bool]] = []


def _fake_set_connection_enabled(db, connection, enabled):
    calls.append((connection.id, enabled))
    connection.enabled = enabled


quota_manager._set_connection_enabled = _fake_set_connection_enabled


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def make_node(db):
    node = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host="1.2.3.4")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


print("--- OpenVPN already active + WireGuard also connects: WireGuard gets kicked ---")
db = make_db()
node = make_node(db)
u = models.User(username="c1", max_concurrent_sessions=1)
db.add(u)
db.commit()
db.refresh(u)
ovpn = models.Connection(user_id=u.id, node_id=node.id, type=models.ConnectionType.openvpn, ppp_username="c1")
wg = models.Connection(user_id=u.id, node_id=node.id, type=models.ConnectionType.wireguard,
                       wg_peer_name="c1-wg", online=True, enabled=True)
db.add_all([ovpn, wg])
db.commit()
db.refresh(ovpn)
db.refresh(wg)
db.add(models.RadiusActiveSession(connection_id=ovpn.id, session_id="s1"))
db.commit()

calls.clear()
toggled = quota_manager.enforce_concurrent_session_limits(db)
db.commit()
db.refresh(wg)
check("exactly one toggle happened", toggled, 1)
check("the WireGuard connection was disabled", wg.enabled, False)
check("...and marked session_limited", wg.session_limited, True)
check("the OpenVPN connection was never touched", any(c[0] == ovpn.id for c in calls), False)

print("\n--- running it again while OpenVPN is still active: no flapping ---")
calls.clear()
toggled_again = quota_manager.enforce_concurrent_session_limits(db)
check("nothing is toggled a second time - it's already correctly kicked", toggled_again, 0)
check("no call was made for the already-kicked connection", calls, [])

print("\n--- OpenVPN disconnects: WireGuard is released back ---")
db.query(models.RadiusActiveSession).filter_by(connection_id=ovpn.id).delete()
db.commit()
calls.clear()
toggled = quota_manager.enforce_concurrent_session_limits(db)
db.commit()
db.refresh(wg)
check("one release toggle happened", toggled, 1)
check("WireGuard is enabled again", wg.enabled, True)
check("...and no longer marked session_limited", wg.session_limited, False)

print("\n--- with NO cap set (max_concurrent_sessions empty), nothing is ever kicked ---")
db2 = make_db()
node2 = make_node(db2)
u2 = models.User(username="c2")  # no max_concurrent_sessions
db2.add(u2)
db2.commit()
db2.refresh(u2)
ovpn2 = models.Connection(user_id=u2.id, node_id=node2.id, type=models.ConnectionType.openvpn, ppp_username="c2")
wg2 = models.Connection(user_id=u2.id, node_id=node2.id, type=models.ConnectionType.wireguard,
                        wg_peer_name="c2-wg", online=True, enabled=True)
db2.add_all([ovpn2, wg2])
db2.commit()
db2.refresh(ovpn2)
db2.add(models.RadiusActiveSession(connection_id=ovpn2.id, session_id="s2"))
db2.commit()
calls.clear()
toggled = quota_manager.enforce_concurrent_session_limits(db2)
check("nothing is touched without a cap", toggled, 0)
check("no calls made", calls, [])

print("\n--- a purchase-linked connection stays kicked if its OWN quota says no, even with room ---")
db3 = make_db()
node3 = make_node(db3)
u3 = models.User(username="c3", max_concurrent_sessions=1)
db3.add(u3)
db3.commit()
db3.refresh(u3)
purchase = models.Purchase(user_id=u3.id, status=models.UserStatus.quota_exceeded, quota_bytes=1, used_bytes=1)
db3.add(purchase)
db3.commit()
db3.refresh(purchase)
ovpn3 = models.Connection(user_id=u3.id, node_id=node3.id, type=models.ConnectionType.openvpn, ppp_username="c3")
wg3 = models.Connection(
    user_id=u3.id, node_id=node3.id, type=models.ConnectionType.wireguard,
    wg_peer_name="c3-wg", online=True, enabled=False, session_limited=True,
    purchase_id=purchase.id,
)
db3.add_all([ovpn3, wg3])
db3.commit()
# No active RadiusActiveSession for ovpn3 - plenty of "room" by session count
# alone, but wg3's OWN purchase already says quota_exceeded.
calls.clear()
toggled = quota_manager.enforce_concurrent_session_limits(db3)
db3.refresh(wg3)
check("it is NOT released - its own quota still says no", toggled, 0)
check("still disabled", wg3.enabled, False)
check("still marked session_limited", wg3.session_limited, True)

print("\n--- _apply_enabled_state: quota enforcement cannot undo an active kick ---")
db4 = make_db()
node4 = make_node(db4)
u4 = models.User(username="c4")
db4.add(u4)
db4.commit()
db4.refresh(u4)
kicked = models.Connection(
    user_id=u4.id, node_id=node4.id, type=models.ConnectionType.wireguard,
    wg_peer_name="c4-wg", enabled=False, session_limited=True,
)
db4.add(kicked)
db4.commit()
calls.clear()
quota_manager._apply_enabled_state(db4, kicked, True)  # quota logic saying "should be active"
check("no call was made - session_limited wins", calls, [])
check("still disabled", kicked.enabled, False)

not_limited = models.Connection(
    user_id=u4.id, node_id=node4.id, type=models.ConnectionType.wireguard,
    wg_peer_name="c4-wg-2", enabled=False, session_limited=False,
)
db4.add(not_limited)
db4.commit()
calls.clear()
quota_manager._apply_enabled_state(db4, not_limited, True)
check("a normal (non-session-limited) connection IS enabled as usual", not_limited.enabled, True)
check("...via the real call path", calls, [(not_limited.id, True)])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
