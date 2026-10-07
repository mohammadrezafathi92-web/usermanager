"""Lifecycle step crash recovery and atomic credential clearing; no network."""
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
from app.services import provisioning_schema, provisioning_transitions as transitions
from app.services.adapter_base import AbsentOutcome, PresentResult, ReadResult, ReadState


def refusal(code, callback):
    try:
        callback()
        raise AssertionError("unexpected success: " + code)
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine):
    label = engine.dialect.name
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    node = models.Node(name="node", type=models.NodeType.mikrotik)
    user = models.User(username="u")
    db.add_all([node, user])
    db.commit()
    nid, uid = node.id, user.id
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))

    def prepare(backend="mikrotik_wg", direction="create"):
        operation = mp.ProvisioningOperation(operation_type="add_connection", business_key=str(uuid.uuid4()),
            request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}", target_user_id=uid,
            state="prepared", forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
        db.add(operation)
        db.flush()
        step = mp.ProvisioningStep(operation_id=operation.id, slot_key="s1", direction=direction,
            step_order=0, backend=backend, node_id=nid,
            protocol="pptp" if backend == "radius_ppp" else "xray" if backend == "xray_ssh" else "wireguard",
            state="staged", staged_wg_private_key="PRIVATE_SENTINEL", staged_password="PASSWORD_SENTINEL",
            staged_xr_uuid="UUID_SENTINEL")
        db.add(step)
        db.commit()
        commits.clear()
        return operation.id, step.id

    oid, sid = prepare()
    row = transitions.begin_remote(db, sid, 0)
    assert row.state == "remote_calling" and row.remote_attempted and row.version == 1 and commits == []
    db.rollback()
    assert db.get(mp.ProvisioningStep, sid).state == "staged"
    transitions.begin_remote(db, sid, 0)
    db.commit()
    refusal("provisioning_step_changed", lambda: transitions.confirm_present(db, sid, 0, PresentResult(True)))
    db.rollback()
    transitions.recover_read(db, sid, 1, ReadResult(ReadState.ABSENT))
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "staged" and row.remote_attempted and row.staged_password == "PASSWORD_SENTINEL"
    transitions.begin_compensation(db, oid, "forward_deadline_exceeded")
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "compensating" and row.staged_wg_private_key is None and row.staged_password is None
    assert row.staged_xr_uuid == "UUID_SENTINEL"
    transitions.confirm_absent(db, sid, row.version, AbsentOutcome.UNVERIFIED)
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "cleanup_required" and db.get(mp.ProvisioningOperation, oid).state == "cleanup_required"
    assert row.staged_xr_uuid == "UUID_SENTINEL" and row.remote_outcome == "unverified"
    transitions.begin_compensation(db, oid, "cancelled_by_recovery")
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "compensating" and row.remote_outcome is None
    transitions.confirm_absent(db, sid, row.version, AbsentOutcome.VERIFIED_ABSENT)
    db.flush()
    assert all(getattr(row, field) is None for field in transitions.SECRETS)
    db.rollback()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "compensating" and row.staged_xr_uuid == "UUID_SENTINEL"
    transitions.confirm_absent(db, sid, row.version, AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT)
    db.commit()
    assert row.state == "removed" and all(getattr(row, field) is None for field in transitions.SECRETS)
    print("PASS", label, "crash absence retains attempted flag; compensation, unknown result, retry and credential clearing are atomic")

    oid, sid = prepare()
    transitions.begin_compensation(db, oid, "node_unavailable")
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "removed" and not row.remote_attempted and row.remote_outcome is None
    assert all(getattr(row, field) is None for field in transitions.SECRETS)
    db.commit()
    print("PASS", label, "never-attempted steps compensate without contacting a node")

    oid, sid = prepare()
    transitions.begin_remote(db, sid, 0)
    db.commit()
    transitions.recover_read(db, sid, 1, ReadResult(ReadState.PRESENT_CONFLICT))
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "cleanup_required" and row.error_code == "remote_identity_conflict"
    assert row.staged_password is None and row.staged_xr_uuid == "UUID_SENTINEL"
    assert db.get(mp.ProvisioningOperation, oid).state == "cleanup_required"
    print("PASS", label, "identity conflict requires cleanup, never automatic deletion or forward completion")

    oid, sid = prepare()
    transitions.begin_remote(db, sid, 0)
    db.commit()
    refusal("provisioning_steps_incomplete", lambda: transitions.mark_remote_complete(db, oid))
    db.rollback()
    transitions.recover_read(db, sid, 1, ReadResult(ReadState.UNREADABLE))
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "remote_calling" and row.next_retry_at is not None
    transitions.recover_read(db, sid, row.version, ReadResult(ReadState.PRESENT_MATCH))
    db.commit()
    transitions.mark_remote_complete(db, oid)
    db.commit()
    conn = models.Connection(user_id=uid, node_id=nid, type=models.ConnectionType.wireguard)
    db.add(conn)
    db.flush()
    row = db.get(mp.ProvisioningStep, sid)
    refusal("provisioning_connection_mismatch", lambda: transitions.activate(db, sid, row.version, 999999))
    refusal("provisioning_connection_mismatch", lambda: transitions.activate(db, sid, row.version, conn.id))
    assert row.staged_wg_private_key == "PRIVATE_SENTINEL" and row.state == "remote_created"
    conn.wg_private_key = row.staged_wg_private_key
    db.flush()
    transitions.activate(db, sid, row.version, conn.id)
    db.flush()
    assert row.state == "active" and row.connection_id == conn.id and all(
        getattr(row, field) is None for field in transitions.SECRETS)
    db.rollback()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "remote_created" and row.staged_password == "PASSWORD_SENTINEL"
    public = transitions.public_step(row)
    assert not set(transitions.SECRETS) & set(public)
    assert "SENTINEL" not in str(public) and "error_sanitized" not in public
    print("PASS", label, "unreadable retries, matching recovery, completion barrier and activation rollback retain safe credentials")

    oid, sid = prepare(backend="radius_ppp")
    refusal("provisioning_step_transition_invalid", lambda: transitions.begin_remote(db, sid, 0))
    db.rollback()
    transitions.mark_remote_complete(db, oid)
    db.commit()
    conn = models.Connection(user_id=uid, node_id=nid, type=models.ConnectionType.pptp,
                             ppp_username="ppp", ppp_password="PASSWORD_SENTINEL")
    db.add(conn)
    db.flush()
    transitions.activate(db, sid, 0, conn.id)
    db.commit()
    row = db.get(mp.ProvisioningStep, sid)
    assert row.state == "active" and not row.remote_attempted
    print("PASS", label, "RADIUS-only steps finalize without a fake remote attempt")

    oid, sid = prepare(backend="xray_ssh")
    transitions.begin_remote(db, sid, 0)
    db.commit()
    transitions.confirm_present(db, sid, 1, PresentResult(False))
    db.commit()
    transitions.mark_remote_complete(db, oid)
    db.commit()
    conn = models.Connection(user_id=uid, node_id=nid, type=models.ConnectionType.xray,
                             xr_uuid="WRONG", xr_flow="")
    db.add(conn)
    db.flush()
    row = db.get(mp.ProvisioningStep, sid)
    refusal("provisioning_connection_mismatch", lambda: transitions.activate(db, sid, row.version, conn.id))
    conn.xr_uuid = row.staged_xr_uuid
    db.flush()
    transitions.activate(db, sid, row.version, conn.id)
    db.commit()
    assert row.state == "active" and row.staged_xr_uuid is None and conn.xr_uuid == "UUID_SENTINEL"
    print("PASS", label, "Xray activation preserves credentials in the exact final connection")

    oid, sid = prepare()
    operation = db.get(mp.ProvisioningOperation, oid)
    operation.operation_type = "purchase"
    operation.approval_uuid = str(uuid.uuid4())
    operation.approval_execution_version = 1
    operation.approval_key_bound = False
    db.commit()
    refusal("provisioning_approval_integration_unavailable", lambda: transitions.begin_remote(db, sid, 0))
    db.rollback()
    operation = db.get(mp.ProvisioningOperation, oid)
    operation.approval_uuid = operation.approval_execution_version = operation.approval_key_bound = None
    operation.forward_deadline = dt.datetime.utcnow() - dt.timedelta(seconds=1)
    db.commit()
    refusal("provisioning_forward_deadline_exceeded", lambda: transitions.begin_remote(db, sid, 0))
    db.rollback()
    print("PASS", label, "unsupported approval integration and expired forward deadline fail closed")
    db.close()


with tempfile.TemporaryDirectory(prefix="um-transitions-") as directory:
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
