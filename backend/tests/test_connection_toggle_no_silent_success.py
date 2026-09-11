"""_set_connection_enabled must never record a connection as toggled unless
the remote node actually confirmed it.

Run:  python3 backend/tests/test_connection_toggle_no_silent_success.py

Reported 2026-09: WireGuard/V2Ray connections kept passing traffic past
their quota, and stayed connected even after the account showed
«اتمام حجم». Root cause - _set_connection_enabled used to set
connection.enabled = enabled unconditionally after its try block,
regardless of whether the WireGuard peer was actually found (looked up by
matching RouterOS's `comment` against Connection.wg_peer_name) or the Xray
panel update actually applied. When the lookup/update silently found
nothing to act on, the DB still recorded success - the panel/bot reported
the service as cut off while the real peer/client kept working exactly as
before, and because _enforce_purchase_limits/_enforce_user_limits only
retry on a status TRANSITION (see their own docstrings), it was never
attempted again.

This locks in the fix: connection.enabled only moves when the node
confirms it, and _reconcile_connection_enabled_state (also new) is what
now catches and retries the ones that didn't."""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import quota_manager
import app.services.mikrotik_client as mikrotik_client

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


def make_node(db):
    node = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host="1.2.3.4",
                        mt_wireguard_interface="wg1")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


# --------------------------------------------------------------- WireGuard
class _FakeMtNoMatch:
    """No peer on the router has a comment matching Connection.wg_peer_name
    - the exact situation (stale name, manual router edit, already gone)
    that used to be silently treated as success."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_peers(self, iface=None):
        return []

    def set_peer_disabled(self, peer_id, disabled):
        raise AssertionError("must never be called when no peer matched")


class _FakeMtMatch:
    def __init__(self, comment="wg-peer-1"):
        self.comment = comment
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_peers(self, iface=None):
        return [{"comment": self.comment, ".id": "*1"}]

    def set_peer_disabled(self, peer_id, disabled):
        self.calls.append((peer_id, disabled))

    def remove_simple_queue(self, name):
        pass


print("--- wireguard: peer not found -> connection.enabled must NOT change ---")
db = make_db()
node = make_node(db)
u = models.User(username="u1")
db.add(u)
db.commit()
db.refresh(u)
conn = models.Connection(user_id=u.id, node_id=node.id, type=models.ConnectionType.wireguard,
                          wg_peer_name="wg-peer-1", enabled=True)
db.add(conn)
db.commit()
db.refresh(conn)

mikrotik_client.MikrotikClient.for_node = staticmethod(lambda n: _FakeMtNoMatch())
quota_manager._set_connection_enabled(db, conn, enabled=False)
db.commit()
db.refresh(conn)
check("connection.enabled stays True (not falsely marked disabled)", conn.enabled, True)

print("\n--- wireguard: peer found -> connection.enabled DOES change ---")
db2 = make_db()
node2 = make_node(db2)
u2 = models.User(username="u2")
db2.add(u2)
db2.commit()
db2.refresh(u2)
conn2 = models.Connection(user_id=u2.id, node_id=node2.id, type=models.ConnectionType.wireguard,
                           wg_peer_name="wg-peer-1", enabled=True)
db2.add(conn2)
db2.commit()
db2.refresh(conn2)

fake_mt = _FakeMtMatch()
mikrotik_client.MikrotikClient.for_node = staticmethod(lambda n: fake_mt)
quota_manager._set_connection_enabled(db2, conn2, enabled=False)
db2.commit()
db2.refresh(conn2)
check("connection.enabled flips to False when the peer really was found", conn2.enabled, False)
check("set_peer_disabled was actually called on the router", len(fake_mt.calls), 1)


# ------------------------------------------------------------------- Xray
class _FakeXrayFail:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set_client_enabled(self, inbound_tag, email, uuid_, flow, enabled):
        return False  # every fallback on the panel side failed


class _FakeXraySucceed:
    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def set_client_enabled(self, inbound_tag, email, uuid_, flow, enabled):
        self.calls.append((email, enabled))
        return True


print("\n--- xray: panel update fails -> connection.enabled must NOT change ---")
db3 = make_db()
node3 = models.Node(name="n3", type=models.NodeType.xray, xr_inbound_tag="proxy")
db3.add(node3)
db3.commit()
db3.refresh(node3)
u3 = models.User(username="u3")
db3.add(u3)
db3.commit()
db3.refresh(u3)
conn3 = models.Connection(user_id=u3.id, node_id=node3.id, type=models.ConnectionType.xray,
                           xr_email="u3@panel", xr_uuid="uuid-3", enabled=True)
db3.add(conn3)
db3.commit()
db3.refresh(conn3)

quota_manager.client_for_node = lambda n: _FakeXrayFail()
quota_manager._set_connection_enabled(db3, conn3, enabled=False)
db3.commit()
db3.refresh(conn3)
check("connection.enabled stays True (xray panel never actually confirmed)", conn3.enabled, True)

print("\n--- xray: panel update succeeds -> connection.enabled DOES change ---")
fake_xray = _FakeXraySucceed()
quota_manager.client_for_node = lambda n: fake_xray
quota_manager._set_connection_enabled(db3, conn3, enabled=False)
db3.commit()
db3.refresh(conn3)
check("connection.enabled flips to False once the panel confirms", conn3.enabled, False)
check("set_client_enabled was actually called", len(fake_xray.calls), 1)


# ---------------------------------------------- reconciliation catches it
print("\n--- _reconcile_connection_enabled_state retries a stuck-on connection ---")
db4 = make_db()
node4 = make_node(db4)
u4 = models.User(username="u4")
db4.add(u4)
db4.commit()
db4.refresh(u4)
pkg = models.Package(name="p", quota_gb=1, price=1)
db4.add(pkg)
db4.commit()
db4.refresh(pkg)
GB = 1024 ** 3
purchase = models.Purchase(user_id=u4.id, package_id=pkg.id, status=models.UserStatus.quota_exceeded,
                            quota_bytes=1 * GB, used_bytes=2 * GB)
db4.add(purchase)
db4.commit()
db4.refresh(purchase)
# Connection is STILL marked enabled even though its purchase is already
# quota_exceeded - simulating the exact drift this whole fix targets (the
# one _set_connection_enabled call at the status transition silently
# "succeeded" without actually reaching the node).
conn4 = models.Connection(user_id=u4.id, node_id=node4.id, purchase_id=purchase.id,
                           type=models.ConnectionType.wireguard, wg_peer_name="wg-peer-4", enabled=True)
db4.add(conn4)
db4.commit()
db4.refresh(conn4)

fake_mt4 = _FakeMtMatch(comment="wg-peer-4")
mikrotik_client.MikrotikClient.for_node = staticmethod(lambda n: fake_mt4)
toggled = quota_manager._reconcile_connection_enabled_state(db4, dt.datetime.utcnow())
db4.commit()
db4.refresh(conn4)
check("reconciliation found and toggled the stuck connection", toggled, 1)
check("connection.enabled is now correctly False", conn4.enabled, False)

print("\n--- _reconcile_connection_enabled_state leaves a correct connection alone ---")
db5 = make_db()
node5 = make_node(db5)
u5 = models.User(username="u5")
db5.add(u5)
db5.commit()
db5.refresh(u5)
conn5 = models.Connection(user_id=u5.id, node_id=node5.id, type=models.ConnectionType.wireguard,
                           wg_peer_name="wg-peer-5", enabled=True)
db5.add(conn5)
db5.commit()
toggled5 = quota_manager._reconcile_connection_enabled_state(db5, dt.datetime.utcnow())
check("a healthy connection with nothing wrong is never touched", toggled5, 0)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("سکوت پس از شکست دیگر ثبت موفقیت نمی‌کند")
