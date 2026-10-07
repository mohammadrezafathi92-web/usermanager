"""Paused parent dispatch cannot run after terminal state, takeover or drift."""
import ast
import datetime as dt
import json
import os
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import gate_locks, provisioning_schema as schema, receipt_void_schema, resource_leases
from app.services import provisioning_installation as installation, provisioning_contracts as contracts
from app.services import provisioning_dispatch_binding as fence
from app.services.provisioning_host import HostIdentity

assert fence.ENDPOINT_FIELDS == contracts._FIELDS
assert fence.XRAY_MODES == contracts.XRAY_MODES
tree = ast.parse(Path(fence.__file__).read_text())
assert not any(isinstance(node, ast.ImportFrom) and node.module and (
    "models" in node.module or node.module in ("database", "sqlalchemy.orm")) for node in ast.walk(tree))
assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
    "commit", "rollback", "flush", "run_action", "connect_ex", "sendto") for node in ast.walk(tree))


def refused(callback):
    try:
        callback()
        raise AssertionError("stale dispatch accepted")
    except fence.DispatchBindingUnavailable:
        pass


def scenario(engine, directory):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    installation_uuid = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    db.rollback()
    exclusive = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid, directory)).acquire(shared=False, timeout=0)
    installation.initialize_off(engine, installation_uuid, exclusive, base_dir=directory)
    exclusive.release()
    mode_hold = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid, directory)).acquire(shared=True, timeout=0)
    identity = HostIdentity("a" * 64, str(uuid.uuid4()))  # Explicit binding unit fixture, NOT host verification.
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", identity.host_id, identity.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.gate_mode_epoch = 1, "enforced", 1
    runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
    runtime.lock_verified_at = dt.datetime.utcnow()
    for kind in ("create_user", "purchase"):
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
    user = models.User(username="existing-customer", telegram_id=42, balance=0)
    other_owner = models.AdminUser(username="other-owner", hashed_password="fixture")
    db.add_all([user, other_owner])
    db.commit()
    all_nodes = []
    try:
        for index, backend in enumerate(("mikrotik_wg", "softether", *fence.XRAY_MODES)):
            node_type = models.NodeType.mikrotik if backend == "mikrotik_wg" else (
                models.NodeType.softether if backend == "softether" else models.NodeType.xray)
            node = models.Node(name="fenced-" + backend, type=node_type, enabled=True,
                mt_wireguard_interface="wg0", mt_host="192.0.2.10", mt_use_ssl=False,
                xr_panel_mode=fence.XRAY_MODES.get(backend), xr_ssh_host="192.0.2.11",
                xr_panel_base_url="https://example.invalid/", xr_inbound_tag="inbound", xr_panel_inbound_id=1,
                se_host="192.0.2.12", se_hub_name="hub", mt_password="management-secret-not-selected")
            db.add(node)
            db.commit()
            all_nodes.append(node.id)
            existing = index == 0
            username = user.username if existing else "new-customer-" + str(index)
            saved = dict(schema_version=1, user=dict(username=username, owner_admin_id=None, telegram_id=42),
                         node_fingerprints={str(node.id): contracts.config_fingerprint(node)})
            op = mp.ProvisioningOperation(operation_type="purchase" if existing else "create_user",
                business_key="binding-" + backend, request_hash="a" * 64, tenant_scope_key="global", actor_kind="system",
                intent=json.dumps(saved), target_user_id=user.id if existing else None,
                username_claim=None if existing else username, state="provisioning",
                forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
            db.add(op)
            db.flush()
            step = mp.ProvisioningStep(operation_id=op.id, slot_key="main", direction="create", step_order=0,
                backend=backend, node_id=node.id, protocol="wireguard" if backend == "mikrotik_wg" else (
                    "softether" if backend == "softether" else "xray"), state="remote_calling", remote_attempted=True,
                wg_interface="wg0" if backend == "mikrotik_wg" else None,
                xr_inbound_tag="inbound" if backend in fence.XRAY_MODES else None,
                xr_panel_inbound_id=1 if backend in fence.XRAY_MODES else None,
                staged_wg_private_key="private-never-selected" if backend == "mikrotik_wg" else None,
                staged_password="password-never-selected" if backend == "softether" else None,
                staged_xr_uuid=str(uuid.uuid4()) if backend in fence.XRAY_MODES else None)
            db.add(step)
            db.commit()
            lease = resource_leases.acquire(db, f"provisioning_op:{op.id}", f"op:{op.id}:1", ttl=300)
            db.commit()
            request = fence.DispatchBinding(installation_uuid, 1, 1, op.id, op.version, step.id, step.version,
                node.id, backend, lease.owner, lease.epoch)
            node_hold = gate_locks.FileLock(gate_locks.node_lock_path(installation_uuid, node.id, directory)).acquire(
                shared=False, timeout=0)
            verify = lambda request=request: fence.revalidate(engine, request, identity, mode_hold, node_hold, base_dir=directory)
            try:
                queries = []
                def collect(connection, cursor, statement, parameters, context, executemany):
                    queries.append(statement)
                event.listen(engine, "before_cursor_execute", collect)
                try:
                    assert verify() == request
                finally:
                    event.remove(engine, "before_cursor_execute", collect)
                assert len(queries) == 1 and queries[0].lstrip().startswith("SELECT ")
                assert all(secret not in queries[0] for secret in ("mt_password", "staged_password", "staged_xr_uuid",
                    "staged_wg_private_key"))
                refused(lambda: verify(replace(request, step_version=request.step_version + 1)))
                refused(lambda: verify(replace(request, operation_version=request.operation_version + 1)))
                refused(lambda: verify(replace(request, lease_epoch=request.lease_epoch + 1)))
                refused(lambda: verify(replace(request, node_id=node.id + 999)))
                refused(lambda: fence.revalidate(engine, request, HostIdentity(identity.host_id, str(uuid.uuid4())),
                    mode_hold, node_hold, base_dir=directory))
                runtime.owner_state = "draining"
                db.commit()
                assert verify() == request  # Existing committed work can finish; this is NOT new admission.
                runtime.owner_state = "active"
                op.forward_deadline = dt.datetime(2000, 1, 1)
                db.commit()
                refused(verify)
                op.forward_deadline = dt.datetime.utcnow() + dt.timedelta(minutes=5)
                db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1)
                    .values(epoch=rv.wallet_runtime_state.c.epoch + 1))
                db.commit()
                refused(verify)
                db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1)
                    .values(epoch=rv.wallet_runtime_state.c.epoch - 1))
                db.commit()
                node.enabled = False
                db.commit()
                refused(verify)
                node.enabled = True
                previous_host = node.mt_host
                node.mt_host = "192.0.2.99"
                db.commit()
                refused(verify)
                node.mt_host = previous_host
                db.commit()
                with engine.begin() as connection:
                    connection.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key == lease.resource_key)
                        .values(leased_until=dt.datetime(2000, 1, 1)))
                refused(verify)
                with engine.begin() as connection:
                    connection.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key == lease.resource_key)
                        .values(leased_until=dt.datetime.utcnow() + dt.timedelta(minutes=5)))
                if existing:
                    user.owner_admin_id = other_owner.id
                    db.commit()
                    refused(verify)
                    user.owner_admin_id = None
                    db.commit()
                # Parent has an old immutable request, but has not spawned a child.
                node_hold.release()
                op.state, op.version = "compensating", op.version + 1
                step.state, step.version = "compensating", step.version + 1
                step.staged_wg_private_key = step.staged_password = None
                node.enabled = False
                db.commit()
                node_hold.acquire(shared=False, timeout=0)
                refused(verify)
                cleanup = replace(request, phase="compensation", operation_version=op.version, step_version=step.version)
                assert verify(cleanup) == cleanup  # Disabled node still permits existing identity cleanup.
                node_hold.release()
                step.state, step.remote_outcome, step.version = "removed", "verified_absent", step.version + 1
                step.staged_wg_private_key = step.staged_password = step.staged_xr_uuid = None
                op.state, op.completed_at, op.username_claim, op.version = "compensated", dt.datetime.utcnow(), None, op.version + 1
                db.commit()
                node_hold.acquire(shared=False, timeout=0)
                marker = []
                for stale in (request, cleanup):
                    try:
                        verify(stale)
                        marker.append("REMOTE WOULD RUN")
                    except fence.DispatchBindingUnavailable:
                        pass
                assert marker == []  # No late recreate/delete after terminal cleanup.
                assert db.query(models.Connection).count() == 0 and db.get(models.User, user.id).balance == 0
                node_hold.release()
                refused(verify)
            finally:
                node_hold.release()
            print("PASS", engine.dialect.name, backend, "fresh binding, DB-clock lease, drift, drain, old parent after compensation")
    finally:
        mode_hold.release()
        db.close()


with tempfile.TemporaryDirectory(prefix="um-dispatch-binding-") as directory:
    local = Path(directory) / "local"
    local.mkdir()
    engine = create_engine("sqlite:///" + str(Path(directory) / "test.db"))
    try:
        scenario(engine, str(local))
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        remote = Path(directory) / "remote"
        remote.mkdir()
        engine = scratch.claim(url)
        try:
            scenario(engine, str(remote))
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
