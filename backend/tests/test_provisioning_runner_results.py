"""Parent result persistence is fenced, reversible and never financial."""
import ast
import datetime as dt
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
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_runner_results as results
from app.services import provisioning_schema, receipt_void_schema, resource_leases
from app.services.provisioning_dispatch_binding import DispatchBinding
from app.services.provisioning_host import HostIdentity
from app.services.remote_action import RemoteActionResult, Outcome


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    try:
        identity = HostIdentity("a" * 64, str(uuid.uuid4()))  # Test fixture, not host attestation.
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", identity.host_id, identity.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
        runtime.ownership_epoch = runtime.gate_mode_epoch = 1
        runtime.gate_mode = "enforced"
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        runtime.lock_verified_at = dt.datetime.utcnow()
        user = models.User(username="result-customer", balance=37)
        node = models.Node(name="result-node", type=models.NodeType.xray)
        db.add_all([user, node])
        db.commit()
        uid, nid, installation = user.id, node.id, runtime.installation_uuid
        db.rollback()
        commits = []
        event.listen(db, "after_commit", lambda session: commits.append(True))

        def prepare(compensation=False):
            op = mp.ProvisioningOperation(operation_type="add_connection", business_key=str(uuid.uuid4()),
                request_hash="0" * 64, tenant_scope_key="global", actor_kind="system", intent="{}",
                target_user_id=uid, state="compensating" if compensation else "provisioning",
                forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
            db.add(op)
            db.flush()
            step = mp.ProvisioningStep(operation_id=op.id, slot_key="one", direction="create", step_order=0,
                backend="xray_ssh", node_id=nid, protocol="xray", remote_attempted=True,
                state="compensating" if compensation else "remote_calling", staged_xr_uuid="SECRET_SENTINEL")
            db.add(step)
            db.commit()
            token = resource_leases.acquire(db, f"provisioning_op:{op.id}", f"op:{op.id}:1", ttl=300)
            db.commit()
            binding = DispatchBinding(installation, 1, 1, op.id, op.version, step.id, step.version,
                nid, "xray_ssh", token.owner, token.epoch, phase="compensation" if compensation else "forward")
            db.rollback()
            commits.clear()
            return binding, token

        def run(binding, token, outcome, *, attempted=False, remote=None, recovery=False):
            action_id = str(uuid.uuid4())
            result = RemoteActionResult(action_id, outcome, write_attempted=attempted, remote_outcome=remote,
                error_sanitized="SECRET_SENTINEL", observed={"irrelevant": "SECRET_SENTINEL"})
            return results.record(db, binding, identity, [token], action_id, result, recovery_read=recovery)

        cases = [(Outcome.SUCCEEDED, True, None, False, "remote_created"),
                 (Outcome.ALREADY_PRESENT_VERIFIED, False, None, False, "remote_created"),
                 (Outcome.ABSENT_VERIFIED, False, "verified_absent", True, "staged"),
                 (Outcome.CONFLICT, False, None, False, "cleanup_required"),
                 (Outcome.UNREADABLE, False, None, False, "remote_calling"),
                 (Outcome.TRANSPORT_ERROR, True, None, False, "remote_calling"),
                 (Outcome.TIMEOUT_UNKNOWN, True, None, False, "remote_calling"),
                 (Outcome.KILLED_UNKNOWN, True, None, False, "remote_calling")]
        for outcome, attempted, remote, recovery, expected in cases:
            binding, token = prepare()
            resource_leases.begin_business(db)
            row = run(binding, token, outcome, attempted=attempted, remote=remote, recovery=recovery)
            assert row.state == expected and row.version == binding.step_version + 1 and commits == []
            assert "SECRET_SENTINEL" not in str(row.error_code)
            db.rollback()
            assert db.get(mp.ProvisioningStep, binding.step_id).state == "remote_calling"
            db.rollback()
            resource_leases.begin_business(db)
            run(binding, token, outcome, attempted=attempted, remote=remote, recovery=recovery)
            db.commit()
            assert commits == [True]
            resource_leases.begin_business(db)
            try:
                run(binding, token, outcome, attempted=attempted, remote=remote, recovery=recovery)
                raise AssertionError("duplicate result accepted")
            except HTTPException as exc:
                assert exc.status_code == 409
            finally:
                db.rollback()

        for outcome, attempted, remote, expected in (
                (Outcome.KILLED_UNKNOWN, True, None, "compensating"),
                (Outcome.CONFLICT, False, "unverified", "cleanup_required"),
                (Outcome.ABSENT_VERIFIED, True, "verified_absent", "removed"),
                (Outcome.ABSENT_VERIFIED, True, "delete_idempotently_absent", "removed")):
            binding, token = prepare(True)
            resource_leases.begin_business(db)
            row = run(binding, token, outcome, attempted=attempted, remote=remote)
            assert row.state == expected
            assert row.staged_xr_uuid == (None if expected == "removed" else "SECRET_SENTINEL")
            assert db.get(mp.ProvisioningOperation, binding.operation_id).state in ("compensating", "cleanup_required")
            db.commit()

        binding, token = prepare()
        action_id = str(uuid.uuid4())
        convergence = RemoteActionResult(action_id, Outcome.UNREADABLE, write_attempted=False,
            error_code="remote_convergence_required")
        resource_leases.begin_business(db)
        row = results.record(db, binding, identity, [token], action_id, convergence, recovery_read=True)
        assert row.state == "staged" and row.remote_attempted and row.staged_xr_uuid == "SECRET_SENTINEL"
        assert row.version == binding.step_version + 1 and row.next_retry_at is None and commits == []
        db.rollback()
        assert db.get(mp.ProvisioningStep, binding.step_id).state == "remote_calling"
        db.rollback()
        for invalid in (convergence, replace(convergence, write_attempted=True), replace(convergence, outcome=Outcome.CONFLICT)):
            resource_leases.begin_business(db)
            try:
                results.record(db, binding, identity, [token], action_id, invalid, recovery_read=invalid is not convergence)
                raise AssertionError("unproven convergence accepted")
            except HTTPException as exc:
                assert exc.status_code == 422
            finally:
                db.rollback()

        binding, token = prepare()
        for invalid, fenced in ((binding, False), (replace(binding, ownership_epoch=2), True),
                                (replace(binding, operation_version=99), True),
                                (replace(binding, lease_epoch=99), True)):
            if fenced:
                resource_leases.begin_business(db)
            try:
                run(invalid, token, Outcome.SUCCEEDED, attempted=True)
                raise AssertionError("unfenced or stale result accepted")
            except HTTPException as exc:
                assert exc.status_code == 409
            finally:
                db.rollback()
        resource_leases.begin_business(db)
        try:
            run(binding, token, Outcome.ABSENT_VERIFIED, remote="verified_absent")
            raise AssertionError("forward absence without recovery accepted")
        except HTTPException as exc:
            assert exc.status_code == 422
        finally:
            db.rollback()
        for changed_result in (RemoteActionResult(str(uuid.uuid4()), Outcome.SUCCEEDED, write_attempted=True),
                               RemoteActionResult(str(uuid.uuid4()), Outcome.UNREADABLE, write_attempted=True)):
            resource_leases.begin_business(db)
            try:
                expected_id = str(uuid.uuid4()) if changed_result.outcome == Outcome.SUCCEEDED else changed_result.action_id
                results.record(db, binding, identity, [token], expected_id, changed_result)
                raise AssertionError("mismatched or contradictory result accepted")
            except HTTPException as exc:
                assert exc.status_code == (409 if changed_result.outcome == Outcome.SUCCEEDED else 422)
            finally:
                db.rollback()
        with engine.begin() as connection:
            connection.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key == token.resource_key)
                .values(leased_until=dt.datetime(2000, 1, 1)))
        resource_leases.begin_business(db)
        try:
            run(binding, token, Outcome.SUCCEEDED, attempted=True)
            raise AssertionError("expired lease accepted")
        except resource_leases.LeaseLost:
            pass
        finally:
            db.rollback()
        assert db.get(mp.ProvisioningStep, binding.step_id).state == "remote_calling"
        assert db.get(models.User, uid).balance == 37
        assert db.scalar(select(func.count()).select_from(models.Connection)) == 0
        assert db.scalar(select(func.count()).select_from(models.LedgerEntry)) == 0
        print("PASS", engine.dialect.name, "eight outcomes, rollback, stale retries, fencing, no delivery or money mutation")
    finally:
        db.close()


tree = ast.parse(Path(results.__file__).read_text())
assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
    "commit", "rollback", "close", "run_action", "capture", "release") for n in ast.walk(tree))
with tempfile.TemporaryDirectory(prefix="um-runner-results-") as directory:
    engine = create_engine("sqlite:///" + directory + "/result.db")
    try:
        scenario(engine)
    finally:
        engine.dispose()
maria = os.environ.get("MARIADB_TEST_URL", "").strip()
if maria:
    engine = scratch.claim(maria)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
