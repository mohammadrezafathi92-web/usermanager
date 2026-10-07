"""Final Connection and staged credential clearing share the caller's transaction."""
import datetime as dt
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import provisioning_schema, provisioning_records as records


def refusal(code, callback):
    try:
        callback()
        raise AssertionError("unexpected success")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    user = models.User(username="owner")
    other = models.User(username="other")
    db.add_all([user, other])
    db.commit()
    uid, other_id = user.id, other.id
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))
    backends = ["mikrotik_wg", "radius_ppp", "softether", *records.XRAY_MODES]
    for backend in backends:
        protocol = "wireguard" if backend == "mikrotik_wg" else "pptp" if backend == "radius_ppp" else "softether" if backend == "softether" else "xray"
        kind = "mikrotik" if backend in ("mikrotik_wg", "radius_ppp") else "softether" if backend == "softether" else "xray"
        node = models.Node(name=backend, type=models.NodeType(kind), enabled=True,
                           xr_panel_mode=records.XRAY_MODES.get(backend, "ssh"), mt_wireguard_interface="wg1")
        purchase = models.Purchase(user_id=uid, quota_bytes=100, package_name_snapshot="snapshot")
        operation = mp.ProvisioningOperation(operation_type="add_connection", business_key=str(uuid.uuid4()),
            request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}", target_user_id=uid,
            state="remote_complete", forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
        db.add_all([node, purchase, operation])
        db.flush()
        step = mp.ProvisioningStep(operation_id=operation.id, slot_key="s", direction="create", step_order=0,
            backend=backend, node_id=node.id, protocol=protocol,
            state="staged" if backend == "radius_ppp" else "remote_created",
            remote_attempted=backend != "radius_ppp", wg_interface="wg1", wg_peer_name="peer",
            wg_public_key="public", wg_client_address="10.0.0.2/32", account_username="account",
            xr_email="email", staged_wg_private_key="private", staged_password="password",
            staged_xr_uuid=str(uuid.uuid4()), flow="", speed_limit_mbps=5, max_concurrent_sessions=2)
        db.add(step)
        db.commit()
        sid, pid = step.id, purchase.id
        prior = db.query(models.Connection).count()
        db.rollback()
        commits.clear()
        refusal("provisioning_connection_mismatch", lambda: records.build_connection_core(
            db, sid, 0, user_id=other_id, purchase_id=pid))
        db.rollback()
        refusal("provisioning_batch_invalid", lambda: records.build_connection_core(
            db, sid, 0, user_id=uid, purchase_id=pid, purchase_batch="x" * 41))
        db.rollback()
        node.enabled = False
        db.flush()
        refusal("node_unavailable", lambda: records.build_connection_core(db, sid, 0, user_id=uid, purchase_id=pid))
        db.rollback()
        secret_field = "staged_wg_private_key" if protocol == "wireguard" else "staged_xr_uuid" if protocol == "xray" else "staged_password"
        setattr(step, secret_field, None)
        db.flush()
        refusal("provisioning_identity_incomplete", lambda: records.build_connection_core(db, sid, 0, user_id=uid, purchase_id=pid))
        db.rollback()
        conn = records.build_connection_core(db, sid, 0, user_id=uid, purchase_id=pid, purchase_batch="batch")
        assert commits == [] and conn.user_id == uid and conn.purchase_id == pid
        assert conn.package_name_snapshot == "snapshot" and conn.max_concurrent_sessions == 2
        assert step.state == "active" and step.connection_id == conn.id
        assert step.staged_wg_private_key is None and step.staged_password is None and step.staged_xr_uuid is None
        if protocol == "wireguard":
            assert conn.wg_private_key == "private" and conn.wg_public_key == "public"
        elif protocol == "xray":
            assert conn.xr_uuid and conn.xr_email == "email"
        else:
            assert conn.ppp_password == "password" and conn.ppp_username == "account"
        db.rollback()
        db.expire_all()
        assert db.query(models.Connection).count() == prior
        step = db.get(mp.ProvisioningStep, sid)
        assert step.staged_password == "password" and step.connection_id is None
        conn = records.build_connection_core(db, sid, 0, user_id=uid, purchase_id=pid, purchase_batch="batch")
        db.commit()
        refusal("provisioning_step_changed", lambda: records.build_connection_core(db, sid, 0, user_id=uid, purchase_id=pid))
        db.rollback()
        assert db.query(models.Connection).count() == prior + 1
    print("PASS", engine.dialect.name, "all nine backends: DB-only final record + credential clear rollback together, retry creates once")
    db.close()


with tempfile.TemporaryDirectory(prefix="um-final-records-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
