"""Quota cutoff terminates existing PPP sessions; disabled WG is not online.

Fake node only. SQLite plus isolated real MariaDB when configured.
"""
import os
import sys
from unittest.mock import patch
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(__file__)]
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
import _mariadb_scratch as scratch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models
from app.services import quota_manager as qm
from app.services.radius_server import UserManagerRadiusServer


def run(engine):
    models.Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        node = models.Node(name="fake", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0")
        user = models.User(username="quota-user")
        db.add_all([node, user]); db.flush()
        exhausted = models.Purchase(user_id=user.id, quota_bytes=100, used_bytes=200,
                                    status=models.UserStatus.quota_exceeded)
        healthy = models.Purchase(user_id=user.id, quota_bytes=1000, used_bytes=10,
                                 status=models.UserStatus.active)
        db.add_all([exhausted, healthy]); db.flush()
        bad = models.Connection(user_id=user.id, node_id=node.id, purchase_id=exhausted.id,
                                type=models.ConnectionType.openvpn, ppp_username="stop", enabled=False,
                                radius_session_id="a", online=True)
        good = models.Connection(user_id=user.id, node_id=node.id, purchase_id=healthy.id,
                                 type=models.ConnectionType.l2tp, ppp_username="keep", enabled=True)
        wg = models.Connection(user_id=user.id, node_id=node.id, purchase_id=exhausted.id,
                               type=models.ConnectionType.wireguard, wg_peer_name="peer", enabled=False)
        db.add_all([bad, good, wg]); db.flush()
        db.add(models.RadiusActiveSession(connection_id=bad.id, session_id="a", last_rx_bytes=100))
        db.commit()

        class Router:
            disabled = True
            def __enter__(self): return self
            def __exit__(self, *exc): pass
            def list_active_ppp_usernames(self): return {"stop", "keep"}
            def list_peers(self, interface):
                return [{"comment": "peer", "rx": 0, "tx": 0,
                         "disabled": self.disabled, "last-handshake": "3s"}]
        router = Router()
        with patch.object(qm.MikrotikClient, "for_node", return_value=router):
            qm.poll_mikrotik_node(db, node)
        assert not wg.online, "disabled peer with fresh handshake is NOT online"
        with patch.object(qm.legacy_remote, "run", side_effect=qm.MikrotikError("offline")):
            assert qm._disconnect_blocked_ppp_sessions(db) == 0
        assert db.query(models.RadiusActiveSession).count() == 1
        assert bad.radius_session_id == "a", "unknown outcome must retain session for retry"

        with patch.object(qm.MikrotikClient, "for_node", return_value=router):
            qm.poll_mikrotik_node(db, node)
        calls = []
        def kick(node_arg, block, **kw):
            calls.append((node_arg.id, block, kw["username"]))
            return True
        with patch.object(qm.legacy_remote, "run", side_effect=kick):
            assert qm._disconnect_blocked_ppp_sessions(db) == 1
        assert calls == [(node.id, "ppp_kick", "stop")], "healthy purchase must remain connected"
        assert not bad.online
        assert db.query(models.RadiusActiveSession).count() == 1, "retain counters for final accounting"
        assert UserManagerRadiusServer._session_delta(db, bad.id, "a", 110, 0) == (0, 10), "late Stop counts only new traffic"
        db.query(models.RadiusActiveSession).delete(synchronize_session=False)
        assert qm._disconnect_blocked_ppp_sessions(db) == 0, "do not replay a stale router observation"

        # No accounting row: RouterOS is still authoritative, so kick it.
        db.info["quota_ppp_live"] = {node.id: {"stop", "keep"}}
        with patch.object(qm.legacy_remote, "run", return_value=False) as remote:
            assert qm._disconnect_blocked_ppp_sessions(db) == 1
            assert remote.call_count == 1
        # Renewal activated in the enforcement pass: don't kick the renewed session.
        bad.enabled = True
        db.info["quota_ppp_live"] = {node.id: {"stop", "keep"}}
        with patch.object(qm.legacy_remote, "run") as remote:
            assert qm._disconnect_blocked_ppp_sessions(db) == 0
            remote.assert_not_called()
        for value in (True, "true", "yes", "1", 1):
            router.disabled = value
            with patch.object(qm.MikrotikClient, "for_node", return_value=router):
                qm.poll_mikrotik_node(db, node)
            assert not wg.online
        router.disabled = False
        with patch.object(qm.MikrotikClient, "for_node", return_value=router):
            qm.poll_mikrotik_node(db, node)
        assert wg.online, "do not hide a remotely enabled peer just because the DB says disabled"
        db.rollback()
    print("PASS", engine.dialect.name, "PPP cutoff/retry/isolation/renewal; WG real disabled state")


engine = create_engine("sqlite://")
try: run(engine)
finally: engine.dispose()
url = os.environ.get("MARIADB_TEST_URL")
if url:
    engine = scratch.claim(url)
    try: run(engine)
    finally: scratch.release(engine)
elif os.environ.get("CI"):
    raise SystemExit("CI requires real MariaDB")
assert not _no_network.attempts
print("all checks passed")
