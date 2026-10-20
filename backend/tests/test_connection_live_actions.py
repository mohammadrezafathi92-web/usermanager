"""Live kick/unban are not durable delete replays; real HTTP/auth, fake remote only."""
import datetime as dt
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
folder = tempfile.TemporaryDirectory(prefix="connection-live-actions-")
os.environ["DATABASE_URL"] = "sqlite:///" + folder.name + "/panel.db"
os.environ.setdefault("SECRET_KEY", "connection-live-actions-local-only")
import _no_network
_no_network.install([])

from fastapi import FastAPI
from fastapi.testclient import TestClient
from app import models, models_provisioning as mp
from app.database import engine, SessionLocal
from app.routers import users
from app.security import create_access_token
from app.services import provisioning_schema, provisioning_durable, user_ops

models.Base.metadata.create_all(engine)
assert provisioning_schema.bootstrap(engine, SessionLocal)["ready"]
with SessionLocal() as db:
    root = models.AdminUser(username="root", hashed_password="unused", is_superadmin=True, role="superadmin")
    seller = models.AdminUser(username="seller", hashed_password="unused", role="seller", permissions="")
    outsider = models.AdminUser(username="outsider", hashed_password="unused", role="admin")
    db.add_all([root, seller, outsider])
    db.flush()
    owner = models.User(username="own", owner_admin_id=root.id, total_quota_bytes=10000, used_bytes=100)
    foreign = models.User(username="foreign", owner_admin_id=outsider.id)
    node = models.Node(name="fake", type=models.NodeType.mikrotik, mt_endpoint_host="invalid.test")
    db.add_all([owner, foreign, node])
    db.flush()
    connection = models.Connection(user_id=owner.id, node_id=node.id, type=models.ConnectionType.openvpn,
        ppp_username="fake", ppp_password="not-real", enabled=True, max_concurrent_sessions=3,
        banned_until=dt.datetime.utcnow() + dt.timedelta(minutes=3), radius_session_id="live")
    other = models.Connection(user_id=foreign.id, node_id=node.id, type=models.ConnectionType.openvpn)
    db.add_all([connection, other])
    db.flush()
    db.add(models.RadiusActiveSession(connection_id=connection.id, session_id="live"))
    db.query(mp.ProvisioningTypeMode).filter_by(operation_type="delete_connection").update({"mode": "durable"})
    db.commit()
    uid, cid, foreign_cid = owner.id, connection.id, other.id
    assert provisioning_durable.is_durable(db, "delete_connection")

app = FastAPI()
app.include_router(users.router)
client = TestClient(app, raise_server_exceptions=False)
headers = {"Authorization": "Bearer " + create_access_token("root")}
path = f"/api/users/{uid}/connections/{cid}"
checks = 0

def check(label, condition):
    global checks
    assert condition, label
    checks += 1
    print("PASS", label)

with patch.object(users, "_prior_delete", side_effect=AssertionError("live action queried delete replay")), \
        patch.object(user_ops.legacy_remote, "run", return_value=True) as remote:
    for action in ("kick", "unban"):
        check(action + " needs authentication", client.post(path + "/" + action).status_code == 401)
        denied = {"Authorization": "Bearer " + create_access_token("seller")}
        check(action + " preserves seller permission gate", client.post(path + "/" + action, headers=denied).status_code == 403)
        denied = {"Authorization": "Bearer " + create_access_token("outsider")}
        check(action + " preserves owner isolation", client.post(path + "/" + action, headers=denied).status_code == 404)
        check(action + " rejects another user's connection", client.post(
            f"/api/users/{uid}/connections/{foreign_cid}/{action}", headers=headers).status_code == 404)
        check(action + " rejects missing connection", client.post(
            f"/api/users/{uid}/connections/999999/{action}", headers=headers).status_code == 404)
    check("denied actions never reach node", remote.call_count == 0)
    response = client.post(path + "/unban", headers=headers)
    check("unban works with durable deletes enabled", response.status_code == 200)
    check("unban returns ConnectionOut and current session count", response.json()["banned_until"] is None
          and response.json()["active_session_count"] == 1 and response.json()["online"] is True)
    check("unban has no remote side effect", remote.call_count == 0)
    response = client.post(path + "/unban", headers=headers)
    check("unban is repeatable", response.status_code == 200)
    with SessionLocal() as db:
        logs = db.query(models.RadiusLimitEventLog).filter_by(connection_id=cid).all()
        check("unban audit remains persisted", len(logs) == 2 and all(
            row.event_type == "unban" and row.user_id == uid and row.active_count == 1 for row in logs))
    response = client.post(path + "/kick", headers=headers)
    check("kick works with durable deletes enabled", response.status_code == 200 and response.json() == {"kicked": True})
    check("kick uses existing gated remote operation", remote.call_count == 1 and remote.call_args.args[1] == "ppp_kick")
    with SessionLocal() as db:
        conn = db.get(models.Connection, cid)
        user = db.get(models.User, uid)
        check("kick clears PPP live sessions", db.query(models.RadiusActiveSession).filter_by(connection_id=cid).count() == 0
              and conn.radius_session_id is None)
        check("kick never deletes or disables credentials or changes quota", conn.enabled and conn.ppp_username == "fake"
              and user.total_quota_bytes == 10000 and user.used_bytes == 100)
        check("no durable delete operation was created", db.query(mp.ProvisioningOperation).count() == 0)
    remote.return_value = False
    check("offline kick remains a clean no-op", client.post(path + "/kick", headers=headers).json() == {"kicked": False})

check("no network", _no_network.attempts == [])
engine.dispose()
folder.cleanup()
print(f"{checks} checks passed")
