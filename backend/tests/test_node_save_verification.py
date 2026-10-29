"""Node-save verification; private SQLite, fake runner seam, no network."""
import os
import sys
import tempfile
import datetime as dt
import contextlib
import json
from unittest.mock import patch

ROOT = tempfile.mkdtemp(prefix="um-node-save-")
os.environ["DATABASE_URL"] = "sqlite:///" + ROOT + "/test.db"
os.environ["UM_LOCK_DIR"] = ROOT + "/locks"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])

from app import models, models_provisioning as mp, schemas
from app.database import engine, SessionLocal
from app.routers import nodes
from app.services import provisioning_schema, legacy_remote, provisioning_control

models.Base.metadata.create_all(engine)
assert provisioning_schema.bootstrap(engine, SessionLocal)["ready"]
with SessionLocal() as db:
    admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    db.add(admin)
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.gate_mode = "enforced"
    runtime.lock_backend = "flock"
    runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.owner_state = "active"
    runtime.owner_host_id, runtime.owner_boot_id = "a" * 64, "b" * 36
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
    db.commit()
    calls = []
    def probe(node):
        calls.append(node.id)
        return {"ready": False, "reason": "read_failed", "cleanup": "clean"}
    with patch.object(legacy_remote, "dispatch_probe", side_effect=probe):
        node = nodes.create_node(schemas.NodeCreate(name="n", type="mikrotik", mt_host="127.0.0.2"), db, admin)
        assert len(calls) == 1, "create must verify immediately"
        from app.services import node_contract_status as status
        assert node.contract_status["state"] == "failed"
        nodes.update_node(node.id, schemas.NodeUpdate(name="renamed"), db, admin)
        assert len(calls) == 1, "harmless name edits must not probe"
        nodes.update_node(node.id, schemas.NodeUpdate(mt_password="new-private-password"), db, admin)
        assert len(calls) == 2, "credential edits must reverify"
        assert "new-private-password" not in str(node.contract_status)
        nodes.list_nodes(db, admin)
        nodes.get_node(node.id, db, admin)
        assert len(calls) == 2, "GET must never probe"
    with patch.object(legacy_remote, "dispatch_probe", return_value={"ready": False, "reason": "timeout", "cleanup": "unknown"}) as runner:
        nodes.verify_node(node.id, db, admin)
        assert node.contract_status["state"] == "cleanup_unknown"
        nodes.verify_node(node.id, db, admin)
        nodes.update_node(node.id, schemas.NodeUpdate(mt_host="127.0.0.3"), db, admin)
        assert runner.call_count == 1, "uncertain cleanup must block ALL repeated probes"
    assert node.mt_host == "127.0.0.3", "save succeeds independently of verification"
    assert node.contract_status["retry_allowed"] is False
    with patch.object(legacy_remote, "dispatch_probe") as runner:
        old = provisioning_control.verify_node_contract(SessionLocal, admin.id, node.id)
        assert old["ready"] is False and not runner.called, "old control endpoint cannot bypass unknown cleanup"
    with patch.object(legacy_remote, "dispatch_probe_cleanup_check") as inspect_peer:
        try:
            nodes.verify_probe_cleanup(node.id, db, admin, None)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409, "changed endpoint cannot be used for cleanup proof"
        else:
            raise AssertionError("cleanup verification on changed endpoint should be refused")
        assert not inspect_peer.called
    safe_identity = "um-probe-0123456789abcdef"
    node.mt_host = "127.0.0.2"  # cleanup proof is only valid for the exact saved endpoint
    db.commit()
    node.contract_check_json = json.dumps({"state": "cleanup_unknown", "cleanup": "unknown",
                                           "reason": "probe_cleanup_unknown", "probe_identity": safe_identity})
    db.commit()
    with patch.object(legacy_remote, "dispatch_probe_cleanup_check", return_value="absent") as inspect_peer:
        result = nodes.verify_probe_cleanup(node.id, db, admin, None)
        assert result.contract_status["state"] == "failed"
        assert result.contract_status["retry_allowed"] is True
        assert result.contract_status["reason"] == "probe_cleanup_verified_absent"
        inspect_peer.assert_called_once_with(node, safe_identity)
    node.contract_check_json = json.dumps({"state": "cleanup_unknown", "cleanup": "unknown",
                                           "reason": "probe_cleanup_unknown", "probe_identity": safe_identity})
    db.commit()
    with patch.object(legacy_remote, "dispatch_probe_cleanup_check", return_value="present") as inspect_peer:
        result = nodes.verify_probe_cleanup(node.id, db, admin, None)
        assert result.contract_status["state"] == "cleanup_unknown"
        assert result.contract_status["retry_allowed"] is False
        assert result.contract_status["cleanup_inspection"] == "present"
        inspect_peer.assert_called_once_with(node, safe_identity)
    # When a runner times out before returning its generated identity, an
    # authorized admin can supply the exact reserved RouterOS comment. Only
    # that identity is read; absence unlocks an explicit new verification.
    node.contract_check_json = json.dumps({"state": "cleanup_unknown", "cleanup": "unknown",
                                           "reason": "probe_cleanup_unknown"})
    db.commit()
    assert nodes.node_contract_status.describe(db, node)["cleanup_manual_identity_required"] is True
    with patch.object(legacy_remote, "dispatch_probe_cleanup_check", return_value="absent") as inspect_peer:
        payload = nodes.ProbeCleanupRequest(probe_identity=safe_identity)
        result = nodes.verify_probe_cleanup(node.id, db, admin, None, payload)
        assert result.contract_status["state"] == "failed"
        assert result.contract_status["retry_allowed"] is True
        assert result.contract_status["reason"] == "probe_cleanup_verified_absent"
        inspect_peer.assert_called_once_with(node, safe_identity)
    node.contract_check_json = json.dumps({"state": "cleanup_unknown", "cleanup": "unknown",
                                           "reason": "probe_cleanup_unknown"})
    db.commit()
    with patch.object(legacy_remote, "dispatch_probe_cleanup_check") as inspect_peer:
        try:
            nodes.verify_probe_cleanup(node.id, db, admin, None,
                                       nodes.ProbeCleanupRequest(probe_identity="not-a-probe"))
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("invalid manual probe identity must be rejected")
        assert not inspect_peer.called
    print("PASS node-save failure, harmless edit, credentials, GET, uncertain cleanup")


def success(node):
    return dict(ready=True, cleanup="clean", contract={"not_exist": []}, server_fingerprint="a" * 64,
                endpoint_fingerprint=status.contracts.contract_rules.endpoint_fingerprint(
                    {"n_" + k: v for k, v in legacy_remote.endpoint(node).items()}))


def more_cases(test_engine, label):
    from sqlalchemy.orm import sessionmaker
    factory = sessionmaker(bind=test_engine)
    models.Base.metadata.create_all(test_engine)
    assert provisioning_schema.bootstrap(test_engine, factory)["ready"]
    with factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        runtime.gate_mode, runtime.lock_backend = "enforced", "flock"
        runtime.lock_verified_at = dt.datetime.utcnow()
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", "a" * 64, "b" * 36
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
        admin = models.AdminUser(username="root-" + label, hashed_password="x", is_superadmin=True)
        db.add(admin)
        db.commit()
        with patch.object(legacy_remote, "dispatch_probe", side_effect=success) as runner:
            node = nodes.create_node(schemas.NodeCreate(name="success", type="mikrotik", mt_host="192.0.2.1"), db, admin)
            assert node.contract_status["ready"] is True
            row = db.get(mp.ProvisioningNodeContract, (node.id, "mikrotik_wg"))
            assert row.state == "ready" and row.verified_by_admin_id == admin.id
            nodes.update_node(node.id, schemas.NodeUpdate(enabled=False, mt_endpoint_host="public.invalid"), db, admin)
            assert runner.call_count == 1 and node.contract_status["ready"] is True
        with patch.object(legacy_remote, "dispatch_probe", return_value={"ready": False, "reason": "password=secret-leak", "cleanup": "clean"}):
            nodes.update_node(node.id, schemas.NodeUpdate(mt_password="rotated"), db, admin)
        assert row.state == "invalidated", "failed credential rotation revokes old contract"
        assert node.contract_status["reason"] == "verification_failed"
        assert "secret-leak" not in str(node.contract_status)
        def edit_credential(snapshot):
            with factory() as other:
                other.get(models.Node, snapshot.id).mt_password = "newer-secret"
                other.commit()
            return success(snapshot)
        with patch.object(legacy_remote, "dispatch_probe", side_effect=edit_credential):
            nodes.verify_node(node.id, db, admin)
        assert node.contract_status["ready"] is False
        assert db.get(mp.ProvisioningNodeContract, (node.id, "mikrotik_wg")).state == "invalidated"
        def overlap(snapshot):
            with factory() as other:
                try:
                    nodes.update_node(snapshot.id, schemas.NodeUpdate(mt_password="overlap"), other, other.get(models.AdminUser, admin.id))
                except Exception as exc:
                    assert getattr(exc, "detail", None) == "node_verification_busy"
                else:
                    raise AssertionError("overlapping save was not refused")
            return success(snapshot)
        with patch.object(legacy_remote, "dispatch_probe", side_effect=overlap):
            nodes.verify_node(node.id, db, admin)
        assert node.contract_status["ready"] is True
        # A request can load an old ORM instance BEFORE obtaining save_lock.
        # An earlier probe completing with unknown cleanup must win, including
        # on MariaDB REPEATABLE READ. Exercise both old and new entry points.
        original_lock = status.save_lock
        for entry in ("control", "nodes"):
            node.contract_check_json = None
            db.commit()
            @contextlib.contextmanager
            def finish_other_probe(session, node_id):
                with factory() as other:
                    current = other.get(models.Node, node_id)
                    current.contract_check_json = json.dumps({"state": "cleanup_unknown", "cleanup": "unknown", "reason": "probe_cleanup_unknown"})
                    other.commit()
                with original_lock(session, node_id):
                    yield
            with patch.object(status, "save_lock", side_effect=finish_other_probe), patch.object(legacy_remote, "dispatch_probe") as runner:
                if entry == "control":
                    result = provisioning_control.verify_node_contract(factory, admin.id, node.id)
                    assert result["ready"] is False
                else:
                    result = nodes.verify_node(node.id, db, admin)
                    assert result.contract_status["state"] == "cleanup_unknown"
                assert not runner.called, "pre-lock stale state must not overwrite cleanup_unknown"
            db.rollback()
            db.refresh(node)
            assert json.loads(node.contract_check_json)["state"] == "cleanup_unknown"
        node.contract_check_json = None
        db.commit()
        temporary_identity = "um-probe-" + "a" * 16 + "@usermanager.local"
        with patch.object(legacy_remote, "dispatch_probe", return_value=dict(ready=False, cleanup="unknown", probe_identity=temporary_identity)):
            nodes.verify_node(node.id, db, admin)
        assert json.loads(node.contract_check_json)["probe_identity"] == temporary_identity
        assert temporary_identity not in str(schemas.NodeOut.model_validate(node).model_dump())
        old = provisioning_control.verify_node_contract(factory, admin.id, node.id)
        assert old["probe_identity"] == temporary_identity
        node.contract_check_json = None
        db.commit()
        # Permission checks run BEFORE acquiring locks or dispatching a probe.
        outsider = models.AdminUser(username="outsider-" + label, hashed_password="x", role="admin")
        seller = models.AdminUser(username="seller-" + label, hashed_password="x", role="seller", parent_admin_id=admin.id)
        db.add_all([outsider, seller])
        db.commit()
        db.add(models.AdminNodeAccess(admin_id=outsider.id, node_id=node.id))
        db.commit()
        with patch.object(legacy_remote, "dispatch_probe") as runner:
            for denied in (outsider, seller):
                try:
                    nodes.verify_node(node.id, db, denied)
                except Exception as exc:
                    assert getattr(exc, "status_code", None) in (403, 404)
                else:
                    raise AssertionError("nonowner probe was allowed")
            assert not runner.called
        # Ownership may change after initial scope lookup but before save_lock.
        # The transferred node remains visible through a grant, but not writable.
        new_owner = models.AdminUser(username="new-owner-" + label, hashed_password="x", role="admin")
        db.add(new_owner)
        db.commit()
        transferred = models.Node(name="unchanged", type=models.NodeType.mikrotik, owner_admin_id=outsider.id, mt_host="192.0.2.9")
        db.add(transferred)
        db.commit()
        transferred_id, previous_owner_id, new_owner_id = transferred.id, outsider.id, new_owner.id
        db.add(models.AdminNodeAccess(admin_id=previous_owner_id, node_id=transferred_id))
        db.commit()
        for entry in ("update", "verify", "delete", "recreate-update", "recreate-verify", "recreate-delete"):
            with factory() as other:
                other.get(models.Node, transferred_id).owner_admin_id = previous_owner_id
                other.commit()
            db.rollback()
            @contextlib.contextmanager
            def transfer_before_lock(session, node_id):
                with factory() as other:
                    if entry.startswith("recreate-"):
                        other.delete(other.get(models.Node, node_id))
                        other.flush()
                        other.add(models.Node(id=node_id, name="unchanged", type=models.NodeType.mikrotik,
                                              owner_admin_id=new_owner_id, mt_host="192.0.2.9"))
                    else:
                        other.get(models.Node, node_id).owner_admin_id = new_owner_id
                    other.commit()
                with original_lock(session, node_id):
                    yield
            with patch.object(status, "save_lock", side_effect=transfer_before_lock), patch.object(legacy_remote, "dispatch_probe", side_effect=success) as runner:
                try:
                    if entry.endswith("update"):
                        nodes.update_node(transferred_id, schemas.NodeUpdate(mt_host="192.0.2.99"), db, outsider)
                    elif entry.endswith("verify"):
                        nodes.verify_node(transferred_id, db, outsider)
                    else:
                        nodes.delete_node(transferred_id, db, outsider, None)
                except Exception as exc:
                    assert getattr(exc, "status_code", None) in (403, 404), entry
                else:
                    raise AssertionError(entry + " authorized a stale pre-lock owner")
                assert not runner.called
            db.rollback()
            with factory() as other:
                current = other.get(models.Node, transferred_id)
                assert current is not None and current.mt_host == "192.0.2.9"
        # Existing NodeOut consumers never see private attempt/credential hashes.
        output = schemas.NodeOut.model_validate(node).model_dump()
        assert "contract_check_token" not in output and "contract_check_fingerprint" not in output
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        runtime.gate_mode = "off"
        db.commit()
        with patch.object(legacy_remote, "dispatch_probe") as runner:
            nodes.update_node(node.id, schemas.NodeUpdate(mt_password="off-rotation"), db, admin)
            assert not runner.called and node.contract_status["state"] == "gate_not_enforced"
        print("PASS", label, "success, fields, invalidation, secret-safe errors, stale completion, lock, gate")


from sqlalchemy import create_engine
more_cases(create_engine("sqlite:///" + ROOT + "/more.db"), "sqlite")
if os.environ.get("MARIADB_TEST_URL"):
    import _mariadb_scratch as scratch
    maria = scratch.claim(os.environ["MARIADB_TEST_URL"])
    try:
        more_cases(maria, "mariadb")
    finally:
        scratch.release(maria)
elif os.environ.get("CI"):
    raise AssertionError("CI requires real MARIADB_TEST_URL")
assert _no_network.attempts == []
