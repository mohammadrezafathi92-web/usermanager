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
from app.services.provisioning_child_database import ChildDatabase
from app.services import provisioning_child_database as child_db
from app.services.provisioning_child_guard import ChildGuard, ChildGuardUnavailable
from app.services import provisioning_child_authority as authority
from app.services import provisioning_parent_dispatch as parent_dispatch
from app.services import provisioning_parent_execute as parent_execute, remote_action
from unittest.mock import patch
from sqlalchemy import text
from fastapi import HTTPException
from app.services.provisioning_host import HostIdentity

assert fence.ENDPOINT_FIELDS == contracts._FIELDS
assert fence.XRAY_MODES == contracts.XRAY_MODES
tree = ast.parse(Path(fence.__file__).read_text())
assert not any(isinstance(node, ast.ImportFrom) and node.module and (
    "models" in node.module or node.module in ("database", "sqlalchemy.orm")) for node in ast.walk(tree))
assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
    "commit", "rollback", "flush", "run_action", "connect_ex", "sendto") for node in ast.walk(tree))
parent_tree = ast.parse(Path(parent_dispatch.__file__).read_text())
assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in (
    "commit", "run_action", "Popen", "connect_ex", "sendto") for node in ast.walk(parent_tree))


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
    reader = None
    try:
        reader = ChildDatabase(engine.url)
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
            verify = lambda request=request: fence.revalidate(reader, request, identity, mode_hold, node_hold, base_dir=directory)
            try:
                def drain_refuses(callback):
                    before = (op.state, op.version, step.state, step.version, step.remote_attempted)
                    db.get(mp.ProvisioningRuntimeState, 1).owner_state = "draining"  # Owned CAS unit fixture.
                    db.commit()
                    db.rollback()
                    resource_leases.begin_business(db)
                    try:
                        callback()
                        raise AssertionError("parent started dispatch during drain")
                    except HTTPException as error:
                        assert error.status_code == 503 and error.detail == "provisioning_draining"
                    finally:
                        db.rollback()
                    assert (op.state, op.version, step.state, step.version, step.remote_attempted) == before
                    db.get(mp.ProvisioningRuntimeState, 1).owner_state = "active"
                    db.commit()

                def guarded_refusal():
                    try:
                        with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory):
                            raise AssertionError("unverified contract accepted")
                    except ChildGuardUnavailable:
                        pass
                guarded_refusal()  # No current contract: no writer permission.
                contract_row = mp.ProvisioningNodeContract(node_id=node.id, backend=backend, state="ready",
                    adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
                    server_fingerprint="b" * 64, contract='{"not_exist":[]}',
                    verified_by_admin_id=other_owner.id, verified_at=dt.datetime.utcnow())
                db.add(contract_row)
                db.commit()
                for field, bad in (("adapter_version", "0" * 40), ("config_fingerprint", "0" * 64),
                                   ("server_fingerprint", "z" * 64), ("contract", "[]")):
                    previous = getattr(contract_row, field)
                    setattr(contract_row, field, bad)
                    db.commit()
                    guarded_refusal()
                    setattr(contract_row, field, previous)
                    db.commit()
                db.rollback()
                drain_refuses(lambda: parent_dispatch.snapshot(db, step.id, request.step_version, identity, [lease], recovery_read=True))
                resource_leases.begin_business(db)
                descriptor = parent_dispatch.snapshot(db, step.id, request.step_version, identity, [lease], recovery_read=True)
                assert descriptor.params["recovery_read"] is True
                assert descriptor.fencing["binding"]["step_version"] == request.step_version
                assert descriptor.fencing["installation_uuid"] == installation_uuid
                assert descriptor.node_config["type"] == node.type.value
                assert descriptor.credential["wg_private_key"] is None
                assert "management-secret-not-selected" not in repr(descriptor)
                assert "private-never-selected" not in repr(descriptor)
                assert "password-never-selected" not in repr(descriptor)
                db.rollback()
                for supplied_version, read_only, supplied_leases, expected in (
                        (request.step_version + 1, True, [lease], "provisioning_step_changed"),
                        (request.step_version, False, [lease], "provisioning_dispatch_state_invalid"),
                        (request.step_version, True, [], "provisioning_lease_scope_invalid")):
                    resource_leases.begin_business(db)
                    try:
                        parent_dispatch.snapshot(db, request.step_id, supplied_version, identity,
                            supplied_leases, recovery_read=read_only)
                        raise AssertionError("invalid parent dispatch accepted")
                    except HTTPException as error:
                        assert error.detail == expected, error.detail
                    finally:
                        db.rollback()
                assert db.get(mp.ProvisioningStep, request.step_id).version == request.step_version
                step = db.get(mp.ProvisioningStep, request.step_id)
                step.state = "staged"
                db.commit()
                drain_refuses(lambda: parent_dispatch.snapshot(db, request.step_id, request.step_version, identity, [lease], recovery_read=False))
                db.rollback()
                resource_leases.begin_business(db)
                first_send = parent_dispatch.snapshot(db, request.step_id, request.step_version, identity,
                    [lease], recovery_read=False)
                assert first_send.params["recovery_read"] is False
                assert first_send.credential["wg_private_key"] is None
                assert first_send.fencing["binding"]["step_version"] == request.step_version + 1
                assert first_send.fencing["binding"]["operation_version"] == request.operation_version + 1
                db.rollback()
                assert db.get(mp.ProvisioningStep, request.step_id).state == "staged"
                assert db.get(mp.ProvisioningStep, request.step_id).version == request.step_version
                db.rollback()
                with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///untrusted-other.db"}), patch.object(
                        parent_execute.remote_runner, "run_action") as launch:
                    try:
                        parent_execute.execute_one(Factory, request.step_id, request.step_version, identity,
                            [lease], recovery_read=False)
                        raise AssertionError("mismatched child source accepted")
                    except HTTPException as error:
                        assert error.detail == "provisioning_runner_configuration_mismatch"
                    launch.assert_not_called()
                with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}):
                    def launched(descriptor):
                        assert engine.pool.checkedout() == 0, "parent session/connection survived across runner I/O"
                        with Factory() as observed:
                            committed = observed.get(mp.ProvisioningStep, request.step_id)
                            assert committed.state == "remote_calling" and committed.version == request.step_version + 1
                            assert observed.query(models.Connection).count() == 0
                        return remote_action.RemoteActionResult(descriptor.action_id, remote_action.Outcome.SUCCEEDED,
                            write_attempted=True)
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=launched):
                        public = parent_execute.execute_one(Factory, request.step_id, request.step_version, identity,
                            [lease], recovery_read=False)
                    assert public["state"] == "remote_created" and public["version"] == request.step_version + 2
                    assert "staged_password" not in public and "private-never-selected" not in str(public)
                    def fixture_state(state):
                        staged = db.get(mp.ProvisioningStep, request.step_id, populate_existing=True)
                        operation = db.get(mp.ProvisioningOperation, request.operation_id, populate_existing=True)
                        staged.state, staged.version = state, request.step_version
                        operation.version = request.operation_version
                        db.commit()
                    fixture_state("staged")
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=RuntimeError("management-secret-not-selected")):
                        failed = parent_execute.execute_one(Factory, request.step_id, request.step_version, identity,
                            [lease], recovery_read=False)
                    assert failed["state"] == "remote_calling" and failed["error_code"] == "remote_result_unknown"
                    assert failed["next_retry_at"] is not None and "management-secret-not-selected" not in str(failed)
                    fixture_state("staged")
                    def wrong_result(descriptor):
                        return remote_action.RemoteActionResult(str(uuid.uuid4()), remote_action.Outcome.SUCCEEDED, write_attempted=True)
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=wrong_result):
                        try:
                            parent_execute.execute_one(Factory, request.step_id, request.step_version, identity,
                                [lease], recovery_read=False)
                            raise AssertionError("another action's result accepted")
                        except HTTPException as error:
                            assert error.detail == "provisioning_runner_result_mismatch"
                    assert db.get(mp.ProvisioningStep, request.step_id, populate_existing=True).state == "remote_calling"
                    assert db.get(mp.ProvisioningStep, request.step_id).version == request.step_version + 1
                    fixture_state("remote_calling")
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=lambda descriptor:
                            remote_action.RemoteActionResult.killed_unknown(descriptor.action_id, "hard_deadline_exceeded")):
                        timed_out = parent_execute.execute_one(Factory, request.step_id, request.step_version, identity,
                            [lease], recovery_read=True)
                    assert timed_out["state"] == "remote_calling" and timed_out["error_code"] == "remote_result_unknown"
                    assert timed_out["next_retry_at"] is not None
                # Restore only the disposable matrix fixture, not real state.
                step = db.get(mp.ProvisioningStep, request.step_id)
                op = db.get(mp.ProvisioningOperation, request.operation_id)
                step.version, op.version = request.step_version, request.operation_version
                step.state = "remote_calling"
                db.commit()
                with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory) as pinned:
                    detached = pinned.contract
                    detached["contract"]["not_exist"].append({"injected": True})
                    assert pinned.contract["contract"]["not_exist"] == []
                    contract_row.version += 1
                    db.commit()
                    try:
                        pinned.check()
                        raise AssertionError("mid-action contract replacement accepted")
                    except ChildGuardUnavailable:
                        pass
                queries = []
                def collect(connection, cursor, statement, parameters, context, executemany):
                    queries.append(statement)
                event.listen(reader._engine, "before_cursor_execute", collect)
                try:
                    assert verify() == request
                finally:
                    event.remove(reader._engine, "before_cursor_execute", collect)
                assert len(queries) == 1 and queries[0].lstrip().startswith("SELECT ")
                assert all(secret not in queries[0] for secret in ("mt_password", "staged_password", "staged_xr_uuid",
                    "staged_wg_private_key"))
                with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory) as guard:
                    assert guard.check() == request
                    # Real SQL/file/advisory guard, synthetic runner-role proof
                    # only in this unit fixture; not an activation certificate.
                    with authority._runner_context(os.getppid()), patch.object(authority, "_protected_parent", return_value=True):
                        with authority._scope(guard):
                            authority.require_write(node.id, backend)
                            contract_row.version += 1
                            db.commit()
                            try:
                                authority.require_write(node.id, backend)
                                raise AssertionError("changed contract retained writer authority")
                            except authority.WriteAuthorityUnavailable:
                                pass
                # A broken guard cannot be reused. Open a fresh one for the
                # independent real advisory-loss/file-lock-loss checks below.
                with ChildGuard(reader, request, identity, mode_hold, node_hold, base_dir=directory) as guard:
                    if engine.dialect.name != "sqlite":
                        assert not guard._connection._connection.in_transaction()
                        name = gate_locks.get_lock_name(installation_uuid, node.id)
                        with reader.connect() as competitor:
                            assert competitor.execute(text(child_db.ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() == 0
                            competitor.end_snapshot()
                            guard._connection._connection.invalidate()  # Real connection loss releases GET_LOCK.
                            assert competitor.execute(text(child_db.ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() == 1
                            competitor.end_snapshot()
                            try:
                                guard.check()
                                raise AssertionError("lost advisory session allowed a writer")
                            except ChildGuardUnavailable:
                                pass
                            assert competitor.execute(text(child_db.ADVISORY_CHECK), dict(name=name)).scalar_one() == 1
                    else:
                        node_hold.release()
                        try:
                            guard.check()
                            raise AssertionError("released file lock allowed a writer")
                        except ChildGuardUnavailable:
                            pass
                        node_hold.acquire(shared=False, timeout=0)
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
                op.forward_deadline = dt.datetime(2000, 1, 1)
                db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1)
                    .values(epoch=rv.wallet_runtime_state.c.epoch + 1))
                db.commit()
                db.rollback()
                drain_refuses(lambda: parent_dispatch.compensation_snapshot(db, cleanup.step_id, cleanup.step_version, identity, [lease]))
                resource_leases.begin_business(db)
                cleanup_dto = parent_dispatch.compensation_snapshot(db, cleanup.step_id, cleanup.step_version, identity, [lease])
                assert cleanup_dto.action_type in (remote_action.ActionType.WG_ENSURE_ABSENT,
                    remote_action.ActionType.XRAY_ENSURE_ABSENT, remote_action.ActionType.SOFTETHER_ENSURE_ABSENT)
                assert cleanup_dto.params["recovery_read"] is False and cleanup_dto.timeouts["hard_deadline"] == 120
                assert cleanup_dto.fencing["binding"]["phase"] == "compensation"
                assert cleanup_dto.fencing["binding"]["step_version"] == cleanup.step_version
                assert cleanup_dto.credential["password"] is None and cleanup_dto.credential["wg_private_key"] is None
                assert "private-never-selected" not in repr(cleanup_dto)
                db.rollback()
                assert verify(cleanup) == cleanup  # Cleanup is allowed beyond the forward window, never beyond its lease.
                resource_leases.begin_business(db)
                try:
                    parent_dispatch.compensation_snapshot(db, cleanup.step_id, cleanup.step_version + 1, identity, [lease])
                    raise AssertionError("stale compensation descriptor accepted")
                except HTTPException as error:
                    assert error.detail == "provisioning_step_changed"
                finally:
                    db.rollback()
                db.execute(rv.wallet_runtime_state.update().where(rv.wallet_runtime_state.c.id == 1)
                    .values(epoch=rv.wallet_runtime_state.c.epoch - 1))
                db.commit()
                old_uuid = step.staged_xr_uuid
                db.rollback()
                with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}):
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=lambda descriptor:
                            remote_action.RemoteActionResult.killed_unknown(descriptor.action_id, "hard_deadline_exceeded")):
                        unknown = parent_execute.execute_compensation(Factory, cleanup.step_id, cleanup.step_version, identity, [lease])
                    assert unknown["state"] == "compensating" and unknown["remote_outcome"] is None
                    assert unknown["error_code"] == "remote_result_unknown" and unknown["next_retry_at"] is not None
                    uncertain = db.get(mp.ProvisioningStep, cleanup.step_id, populate_existing=True)
                    assert uncertain.staged_xr_uuid == old_uuid
                    uncertain.version, uncertain.next_retry_at, uncertain.error_code = cleanup.step_version, None, None
                    db.commit()
                    def cleaned(descriptor):
                        assert engine.pool.checkedout() == 0
                        assert descriptor.fencing["binding"]["phase"] == "compensation"
                        with Factory() as observed:
                            assert observed.get(mp.ProvisioningStep, cleanup.step_id).state == "compensating"
                        return remote_action.RemoteActionResult(descriptor.action_id, remote_action.Outcome.ABSENT_VERIFIED,
                            write_attempted=True, remote_outcome="verified_absent")
                    with patch.object(parent_execute.remote_runner, "run_action", side_effect=cleaned):
                        result = parent_execute.execute_compensation(Factory, cleanup.step_id, cleanup.step_version, identity, [lease])
                    assert result["state"] == "removed" and result["remote_outcome"] == "verified_absent"
                    assert result["version"] == cleanup.step_version + 1
                    assert db.get(mp.ProvisioningOperation, cleanup.operation_id, populate_existing=True).state == "compensating"
                    row = db.get(mp.ProvisioningStep, cleanup.step_id, populate_existing=True)
                    assert row.staged_xr_uuid is None and row.staged_password is None
                    # Reset ONLY this owned scratch fixture for the terminal-state fence test below.
                    row.state, row.version, row.remote_outcome, row.staged_xr_uuid = "compensating", cleanup.step_version, None, old_uuid
                    db.commit()
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
        if reader is not None:
            reader.dispose()


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
