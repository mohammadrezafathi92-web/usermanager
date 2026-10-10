"""Private narrow approval T1: one commit or no approval/operation change."""
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

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import bot_auth, provisioning_approval_binding as binding
from app.services import provisioning_approval_t1 as t1, provisioning_finalization as final, provisioning_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_schema
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    identity = HostIdentity("a" * 64, str(uuid.uuid4()))
    principal = bot_auth.BotPrincipal.internal(None)
    with Factory() as db:
        db.add_all([models.PanelSettings(id=1), models.Package(
            name="P", quota_gb=1, duration_days=30, price=100)])
        db.commit()
        package_id = db.query(models.Package.id).scalar()
        intents = [ri.ApprovalIntent(pending_source_instance_id="t1-test", pending_local_id=n,
            kind="new", target_username=f"new-{n}", amount=100, package_id=package_id,
            claimed_telegram_id=10000 + n) for n in (1, 2, 3)]
        approvals = [registration.register(db, principal, value, approval_mode="auto") for value in intents]
        for approval in approvals:
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == approval.approval_uuid).values(
                registered_under_mode="required", completeness_generation=1))
        db.get(mp.ProvisioningTypeMode, "create_user").mode = "durable"
        host = db.get(mp.ProvisioningRuntimeState, 1)
        installation = host.installation_uuid
        host.owner_state = "active"
        host.owner_host_id = identity.host_id
        host.owner_boot_id = identity.boot_id
        host.owner_claimed_at = host.owner_heartbeat_at = dt.datetime.utcnow()
        host.ownership_epoch = 1
        host.lock_backend = "flock"
        host.lock_verified_at = dt.datetime.utcnow()
        host.gate_mode = "enforced"
        db.commit()
    kwargs = dict(tenant_scope_key="shared", identity=identity,
        ownership_epoch=1, installation_uuid=installation)

    def call(db, index):
        approval = approvals[index]
        # No activation API is called: the effective required mode is a
        # controlled fixture for the otherwise private execution path.
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            return t1.prepare(db, approval.approval_uuid, approval.execution_token,
                principal, intents[index], **kwargs)

    # Failure exactly at the caller's single commit must roll back both
    # sides, including the provisional operation and its resource lease.
    with Factory() as db:
        operation_id, leases, replay = call(db, 0)
        assert operation_id and leases and not replay
        assert db.query(mp.ProvisioningOperation).count() == 1
        row = db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approvals[0].approval_uuid)).mappings().one()
        assert row["state"] == "mutating"
        def crash(_session):
            raise RuntimeError("commit crash")
        event.listen(db, "before_commit", crash)
        try:
            try:
                db.commit()
            except RuntimeError as exc:
                assert str(exc) == "commit crash"
            else:
                raise AssertionError("commit unexpectedly succeeded")
        finally:
            event.remove(db, "before_commit", crash)
            db.rollback()
    with Factory() as db:
        assert db.query(mp.ProvisioningOperation).count() == 0
        row = db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approvals[0].approval_uuid)).mappings().one()
        assert row["state"] == "registered"
    with Factory() as db:
        commits = []
        event.listen(db, "after_commit", lambda _session: commits.append(True))
        first_id, first_leases, replay = call(db, 0)
        assert first_leases and not replay
        assert commits == [], "T1 committed inside its helper"
        db.commit()
        assert commits == [True], commits
    with Factory() as db:
        second_id, second_leases, replay = call(db, 0)
        assert (second_id, second_leases, replay) == (first_id, (), True)
        db.rollback()
    with Factory() as db:
        assert db.query(mp.ProvisioningOperation).count() == 1
        frozen = final._intent(db.get(mp.ProvisioningOperation, first_id))
        assert frozen.package is not None
        assert frozen.quota_bytes == frozen.package.quota_bytes == 1024 ** 3
        assert frozen.duration_days == frozen.package.duration_days == 30
        row = db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approvals[0].approval_uuid)).mappings().one()
        assert row["state"] == "mutating"
    # The second approval still has its own independent T1.
    with Factory() as db:
        own_id, own_leases, replay = call(db, 1)
        assert own_id != first_id and own_leases and not replay
        db.commit()
    # A drift detected after provisional operation insertion must not leave
    # an orphan operation or move the approval when the caller rolls back.
    with Factory() as db:
        db.get(models.Package, package_id).quota_gb = 2
        db.commit()
    with Factory() as db:
        try:
            call(db, 2)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (409, "manifest_precondition_failed")
        else:
            raise AssertionError("changed manifest accepted")
        db.rollback()
    with Factory() as db:
        assert db.query(mp.ProvisioningOperation).count() == 2
        row = db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approvals[2].approval_uuid)).mappings().one()
        assert row["state"] == "registered"
        db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "enforced"
        db.commit()
    with Factory() as db:
        try:
            t1.prepare(db, approvals[2].approval_uuid, approvals[2].execution_token,
                principal, intents[2], **kwargs)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (409, "approval_mode_changed")
        else:
            raise AssertionError("T1 accepted changed approval mode")
        db.rollback()
        db.get(models.Package, package_id).quota_gb = 1
        db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "shadow"
        db.commit()
    with Factory() as db:
        try:
            call(db, 2)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (503, "gate_not_enforced")
        else:
            raise AssertionError("T1 accepted shadow gate")
        db.rollback()
    with Factory() as db:
        assert db.query(mp.ProvisioningOperation).count() == 2
        row = db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approvals[2].approval_uuid)).mappings().one()
        assert row["state"] == "registered"
    print("PASS", engine.dialect.name, "narrow atomic approval T1")


with tempfile.TemporaryDirectory(prefix="um-p6-approval-t1-") as directory:
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
