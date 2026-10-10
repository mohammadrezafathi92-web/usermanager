"""Private deletion coordinator: one bounded step, no live caller/network."""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_schema, receipt_void_schema, wallet_accounts
from app.services import provisioning_contracts as contracts, provisioning_delete_preparation as prep
from app.services import provisioning_deletion_worker as worker, provisioning_parent_execute as execute
from app.services import provisioning_due_deletions as due
from app.services import resource_leases as leases, remote_action
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        installation = runtime.installation_uuid
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
        runtime.ownership_epoch, runtime.gate_mode_epoch, runtime.gate_mode = 1, 1, "enforced"
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.get(mp.ProvisioningTypeMode, "delete_connection").mode = "durable"
        db.get(mp.ProvisioningTypeMode, "delete_purchase").mode = "durable"
        admin = models.AdminUser(username="owner", hashed_password="test", is_superadmin=True)
        user = wallet_accounts.create_user_with_wallet(db, username="delete-customer", balance=101)
        node = models.Node(name="fake-node", type=models.NodeType.mikrotik, enabled=False,
            mt_wireguard_interface="wg0", mt_host="192.0.2.1")
        db.add_all([admin, node])
        db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=admin.id,
            verified_at=dt.datetime.utcnow()))
        purchase = models.Purchase(user_id=user.id, quota_bytes=1000)
        db.add(purchase)
        db.flush()
        uid, nid, pid = user.id, node.id, purchase.id
        db.commit()
    expected = dict(installation_uuid=installation, ownership_epoch=1)

    def new_request():
        with Factory() as db:
            row = models.Connection(user_id=uid, purchase_id=pid, node_id=nid,
                type=models.ConnectionType.wireguard, enabled=True, wg_peer_name="peer-" + uuid.uuid4().hex[:8],
                wg_public_key="PUBLIC", wg_client_address="10.0.0.2/32")
            db.add(row)
            db.commit()
            return row.id, prep.DeleteRequest(resource_kind="connection", resource_id=row.id,
                tenant_scope_key="shared", business_key=str(uuid.uuid4()))

    configured = {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}
    def absence(dto):
        assert engine.pool.checkedout() == 0, "parent DB session survived remote I/O"
        assert dto.credential["password"] is None and dto.credential["wg_private_key"] is None
        assert dto.action_type == remote_action.ActionType.WG_ENSURE_ABSENT
        # The shared customer fence is free during I/O, but the operation
        # fence is not: another mutation can proceed without stealing this
        # deletion's right to record its result.
        with Factory() as other:
            leases.begin_business(other)
            identity_id = other.execute(rv.wallet_accounts.select().where(
                rv.wallet_accounts.c.user_id == uid)).mappings().one()["customer_identity_id"]
            shared = leases.acquire(other, f"customer_identity:{identity_id}", "op:900000:1")
            try:
                leases.acquire(other, f"provisioning_op:{dto.fencing['binding']['operation_id']}", "op:900000:1")
                raise AssertionError("operation lease was released during remote I/O")
            except leases.LeaseBusy:
                pass
            other.commit()
            assert leases.release(other, shared)
            other.commit()
        return remote_action.RemoteActionResult(dto.action_id, remote_action.Outcome.ABSENT_VERIFIED,
            write_attempted=True, remote_outcome="verified_absent")

    with patch.dict(os.environ, configured), patch.object(execute.remote_runner,
            "run_action", side_effect=absence) as launch:
        cid, request = new_request()
        result = worker.start_once(Factory, request, host, **expected)
        assert result["status"] == "step_recorded" and result["step"]["state"] == "removed"
        oid = result["operation_id"]
        assert launch.call_count == 1
        with Factory() as db:
            assert db.get(models.Connection, cid).enabled is False
            assert db.get(mp.ProvisioningOperation, oid).state == "provisioning"
        assert worker.resume_one(Factory, oid, host, **expected)["state"] == "completed"
        with Factory() as db:
            assert db.get(models.Connection, cid) is None
            assert db.get(models.User, uid).balance == 101
            assert db.get(mp.ProvisioningOperation, oid).state == "completed"
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "terminal"
        assert worker.start_once(Factory, request, host, **expected)["status"] == "terminal"
        assert launch.call_count == 1, "terminal replay launched another child"

    cid, request = new_request()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action",
            side_effect=lambda dto: remote_action.RemoteActionResult.killed_unknown(
                dto.action_id, "hard_deadline_exceeded")) as launch:
        result = worker.start_once(Factory, request, host, **expected)
        assert result["step"]["state"] == "remote_calling"
        oid = result["operation_id"]
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "waiting"
        assert launch.call_count == 1
    with Factory() as db:
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
        assert step.remote_attempted and step.next_retry_at is not None
        step.next_retry_at = None  # Only this owned disposable scratch row.
        db.commit()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action", side_effect=absence):
        assert worker.resume_one(Factory, oid, host, **expected)["step"]["state"] == "removed"
    assert worker.resume_one(Factory, oid, host, **expected)["state"] == "completed"
    with Factory() as db:
        assert db.get(models.Connection, cid) is None

    cid, request = new_request()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action",
            side_effect=lambda dto: remote_action.RemoteActionResult(dto.action_id,
                remote_action.Outcome.CONFLICT, write_attempted=False, remote_outcome="unverified")) as launch:
        result = worker.start_once(Factory, request, host, **expected)
        oid = result["operation_id"]
        assert result["state"] == "cleanup_required"
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "manual_recovery_required"
        assert launch.call_count == 1
    with Factory() as db:
        assert db.get(models.Connection, cid) is not None
        assert db.get(models.Connection, cid).enabled is False

    cid, request = new_request()
    with Factory() as db:
        leases.begin_business(db)
        prepared = prep.prepare(db, request, identity=host, **expected)
        oid, original = prepared.operation.id, prepared.leases
        db.commit()
    with Factory() as db:
        for token in original:
            assert leases.release(db, token)
        db.commit()
    with Factory() as db:
        db.get(mp.ProvisioningRuntimeState, 1).owner_state = "draining"
        db.commit()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action") as launch:
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "draining"
        launch.assert_not_called()
    with Factory() as db:
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
        assert step.state == "staged" and not step.remote_attempted
        assert db.get(models.Connection, cid).enabled is False
        db.get(mp.ProvisioningRuntimeState, 1).owner_state = "active"
        db.commit()
    assert due.select_due(Factory, host, after_id=oid - 1, **expected) == (oid,)
    assert due.select_due(Factory, host, after_id=0, **expected) == (oid,), "manual cleanup/terminal rows were selected"
    held = worker.reacquire(Factory, oid, host, **expected)
    assert due.select_due(Factory, host, after_id=oid - 1, **expected) == ()
    with Factory() as db:
        for token in held:
            assert leases.release(db, token)
        db.commit()
    with Factory() as db:
        db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one().next_retry_at = (
            dt.datetime.utcnow() + dt.timedelta(days=1))
        db.commit()
    assert due.select_due(Factory, host, after_id=oid - 1, **expected) == ()
    with Factory() as db:
        db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one().next_retry_at = None
        db.commit()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner,
            "run_action", side_effect=absence):
        selected = due.recover_due_once(Factory, host, after_id=oid - 1, **expected)
        assert selected["selection_cursor"] == oid and selected["step"]["state"] == "removed"
    assert due.recover_due_once(Factory, host, after_id=oid - 1, **expected)["state"] == "completed"
    assert due.recover_due_once(Factory, host, after_id=oid - 1, **expected)["status"] == "idle"
    with Factory() as db:
        assert db.get(models.Connection, cid) is None
    # One purchase with mixed WireGuard and local-RADIUS PPP removal. The
    # latter is DB-only; it must never run a child, skip a preceding step or
    # remove the customer or sibling purchase.
    with Factory() as db:
        group = models.Purchase(user_id=uid, quota_bytes=2000)
        db.add(group)
        db.flush()
        wg = models.Connection(user_id=uid, purchase_id=group.id, node_id=nid,
            type=models.ConnectionType.wireguard, enabled=True,
            wg_peer_name="group-peer", wg_public_key="GROUP-PUBLIC", wg_client_address="10.0.0.3/32")
        ppp = models.Connection(user_id=uid, purchase_id=group.id, node_id=nid,
            type=models.ConnectionType.pptp, enabled=True, ppp_username="group-ppp")
        db.add_all([wg, ppp])
        db.commit()
        group_id, wg_id, ppp_id = group.id, wg.id, ppp.id
    request = prep.DeleteRequest(resource_kind="purchase", resource_id=group_id,
        tenant_scope_key="shared", business_key=str(uuid.uuid4()))
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner,
            "run_action", side_effect=absence) as launch:
        first = worker.start_once(Factory, request, host, **expected)
        assert first["step"]["state"] == "removed"
        second = worker.resume_one(Factory, first["operation_id"], host, **expected)
        assert second["step"]["state"] == "removed"
        assert second["step"]["backend"] == "radius_ppp"
        assert launch.call_count == 1, "PPP removal invoked a remote child"
        complete = worker.resume_one(Factory, first["operation_id"], host, **expected)
        assert complete["state"] == "completed" and launch.call_count == 1
    with Factory() as db:
        assert db.get(models.Purchase, group_id) is None
        assert db.get(models.Connection, wg_id) is None and db.get(models.Connection, ppp_id) is None
        assert db.get(models.Purchase, pid) is not None
        assert db.get(models.User, uid).balance == 101
    assert _no_network.attempts == []
    print("PASS", engine.dialect.name, "private deletion worker, retry, conflict, drain and T_final")


with tempfile.TemporaryDirectory(prefix="um-delete-worker-") as directory:
    engine = create_engine("sqlite:///" + directory + "/worker.db")
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
