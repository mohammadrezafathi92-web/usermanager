"""Read-only deletion binding: exact committed scope/identity, never dispatch."""
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import gate_locks, provisioning_schema, receipt_void_schema, resource_leases
from app.services import provisioning_deletion as deletion, provisioning_dispatch_binding as fence
from app.services import provisioning_installation as installation
from app.services.provisioning_child_database import ChildDatabase
from app.services.provisioning_host import HostIdentity
from app.services.marzneshin_client import sanitize_username
from app.services import provisioning_child_recovery as recovery

# This batch only admits a committed readonly binding. It MUST NOT
# accidentally make the existing presence/compensation action executable.
try:
    recovery._validate(None, None, phase="deletion")
    raise AssertionError("deletion runner enabled before action/result integration")
except recovery.RecoveryUnavailable as error:
    assert str(error) == "child_recovery_binding_invalid"


def refused(callback):
    try:
        callback()
        raise AssertionError("stale deletion binding accepted")
    except fence.DispatchBindingUnavailable:
        pass


def scenario(engine, directory):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        namespace = runtime.installation_uuid
        db.rollback()
        exclusive = gate_locks.FileLock(gate_locks.mode_lock_path(namespace, directory)).acquire(shared=False, timeout=0)
        installation.initialize_off(engine, namespace, exclusive, base_dir=directory)
        exclusive.release()
        host = HostIdentity("a" * 64, str(uuid.uuid4()))
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.ownership_epoch, runtime.gate_mode_epoch, runtime.gate_mode = 1, 1, "enforced"
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        for kind in ("delete_connection", "delete_purchase", "delete_user"):
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        outsider = models.User(username="other-customer")
        db.add(outsider)
        db.commit()
        mode = gate_locks.FileLock(gate_locks.mode_lock_path(namespace, directory)).acquire(shared=True, timeout=0)
        reader = ChildDatabase(engine.url)
        try:
            cases = [(backend, "connection") for backend in ("mikrotik_wg", "softether", *fence.XRAY_MODES)]
            cases.extend(("mikrotik_wg", kind) for kind in ("purchase", "user"))
            for backend, kind in cases:
                protocol = "wireguard" if backend == "mikrotik_wg" else "softether" if backend == "softether" else "xray"
                node = models.Node(name="binding-" + backend, type=models.NodeType.mikrotik if protocol == "wireguard"
                    else models.NodeType.softether if protocol == "softether" else models.NodeType.xray,
                    enabled=False, mt_wireguard_interface="wg0", mt_host="192.0.2.1", mt_use_ssl=False,
                    xr_panel_mode=fence.XRAY_MODES.get(backend), xr_inbound_tag="tag", xr_panel_inbound_id=3,
                    xr_panel_base_url="https://example.invalid", se_host="192.0.2.2", se_hub_name="hub")
                user = models.User(username="bound-" + uuid.uuid4().hex[:10], status=models.UserStatus.disabled
                    if kind == "user" else models.UserStatus.active, purchases_blocked=kind == "user")
                db.add_all([node, user])
                db.flush()
                purchase = models.Purchase(user_id=user.id, quota_bytes=100)
                db.add(purchase)
                db.flush()
                connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
                    type=models.ConnectionType(protocol), enabled=False, wg_peer_name="peer", wg_public_key="PUBLIC",
                    wg_client_address="10.0.0.2/32", wg_private_key="PRIVATE-NOT-QUERIED", ppp_username="account",
                    ppp_password="PASSWORD-NOT-QUERIED", xr_email="Customer.Email@Example", xr_uuid="UUID-IDENTITY",
                    xr_flow="xtls-rprx-vision")
                db.add(connection)
                db.flush()
                target = connection.id if kind == "connection" else purchase.id if kind == "purchase" else user.id
                operation = mp.ProvisioningOperation(operation_type="delete_" + kind, business_key=str(uuid.uuid4()),
                    request_hash="a" * 64, tenant_scope_key="shared", actor_kind="system", target_user_id=user.id,
                    intent=deletion.snapshot(kind, target, [connection], purchase_ids=[purchase.id]),
                    state="provisioning", forward_deadline=dt.datetime.utcnow() - dt.timedelta(days=1), wallet_epoch_at_start=0)
                db.add(operation)
                db.flush()
                step = mp.ProvisioningStep(operation_id=operation.id, connection_id=connection.id, node_id=node.id,
                    slot_key="remove", step_order=0, direction="remove", protocol=protocol, backend=backend,
                    state="remote_calling", remote_attempted=True, wg_interface=node.mt_wireguard_interface,
                    wg_peer_name=connection.wg_peer_name, wg_public_key=connection.wg_public_key,
                    wg_client_address=connection.wg_client_address, xr_email=connection.xr_email,
                    staged_xr_uuid=connection.xr_uuid if protocol == "xray" else None,
                    xr_inbound_tag=node.xr_inbound_tag, xr_panel_inbound_id=node.xr_panel_inbound_id,
                    flow=connection.xr_flow if protocol == "xray" else None,
                    account_username=sanitize_username(connection.xr_email) if backend == "marzneshin" else connection.ppp_username)
                db.add(step)
                db.commit()
                lease = resource_leases.acquire(db, f"provisioning_op:{operation.id}", f"op:{operation.id}:1", ttl=300)
                db.commit()
                binding = fence.DispatchBinding(namespace, 1, 1, operation.id, operation.version, step.id, step.version,
                    node.id, backend, lease.owner, lease.epoch, "deletion")
                hold = gate_locks.FileLock(gate_locks.node_lock_path(namespace, node.id, directory)).acquire(shared=False, timeout=0)
                verify = lambda candidate=binding: fence.revalidate(reader, candidate, host, mode, hold, base_dir=directory)
                try:
                    assert verify() == binding  # Disabled node/expired forward deadline do not abandon deletion.
                    refused(lambda: verify(replace(binding, phase="forward")))
                    refused(lambda: verify(replace(binding, phase="compensation")))
                    refused(lambda: verify(replace(binding, operation_version=binding.operation_version + 1)))
                    refused(lambda: verify(replace(binding, step_version=binding.step_version + 1)))
                    refused(lambda: verify(replace(binding, ownership_epoch=2)))
                    refused(lambda: verify(replace(binding, lease_epoch=lease.epoch + 1)))
                    def drift(row, field, value, **companions):
                        updates = {field: value, **companions}
                        previous = {name: getattr(row, name) for name in updates}
                        for name, supplied in updates.items():
                            setattr(row, name, supplied)
                        db.commit()
                        refused(verify)
                        for name, prior in previous.items():
                            setattr(row, name, prior)
                        db.commit()
                        assert verify() == binding
                    drift(connection, "enabled", True)
                    drift(connection, "user_id", outsider.id)
                    drift(connection, "xr_flow", "changed-flow")
                    drift(node, "mt_host", "changed-endpoint")
                    drift(operation, "state", "completed", completed_at=dt.datetime.utcnow())
                    drift(operation, "operation_type", "create_user")
                    drift(step, "state", "staged")
                    drift(step, "direction", "create")
                    if protocol == "wireguard":
                        drift(step, "wg_public_key", "different-peer")
                        drift(connection, "wg_public_key", "different-source")
                    elif protocol == "softether":
                        drift(step, "account_username", "different-account")
                    else:
                        drift(step, "flow", "different-flow")
                        drift(step, "staged_xr_uuid", "different-object")
                        if backend == "marzneshin":
                            drift(step, "account_username", "wrong-normalized-name")
                    if kind == "user":
                        drift(user, "purchases_blocked", False)
                        drift(user, "status", models.UserStatus.active)
                    saved = json.loads(operation.intent)
                    for changed in ({**saved, "resource_id": target + 100000}, {**saved, "resource_id": True},
                                    {**saved, "resource_kind": "unknown"}, {**saved, "connection_fingerprints": {}}):
                        drift(operation, "intent", json.dumps(changed))
                    # No loss of versions: this is purely a SELECT proof,
                    # never a marker write, remote result or record removal.
                    assert (operation.version, step.version, step.state) == (0, 0, "remote_calling")
                    assert db.get(models.Connection, connection.id) is not None
                finally:
                    hold.release()
                    db.rollback()
                    assert resource_leases.release(db, lease)
                    db.commit()
            statement = str(fence.dispatch_statement(engine.dialect.name, deletion=True))
            assert "staged_password" not in statement and "wg_private_key" not in statement
            assert "mt_password" not in statement and "xr_ssh_password" not in statement
            assert "staged_xr_uuid" not in str(fence.dispatch_statement(engine.dialect.name))
            assert _no_network.attempts == []
        finally:
            reader.dispose()
            mode.release()
    print("PASS", engine.dialect.name, "readonly deletion binding, eight adapters, three scopes and drift fences")


with tempfile.TemporaryDirectory(prefix="um-delete-binding-") as directory:
    engine = create_engine("sqlite:///" + directory + "/binding.db")
    try:
        scenario(engine, directory)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL")
if url:
    engine = scratch.claim(url)
    try:
        with tempfile.TemporaryDirectory(prefix="um-delete-binding-maria-") as directory:
            scenario(engine, directory)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("real MariaDB mandatory in CI")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
