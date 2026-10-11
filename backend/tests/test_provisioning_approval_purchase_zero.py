"""A receipt-paid existing-user purchase with no remote step stays atomic."""
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
from app.services import provisioning_approval_purchase_zero as purchase_zero
from app.services import provisioning_schema, receipt_approval_intent as ri
from app.services import receipt_approval_registration as registration, receipt_void_schema
from app.services import wallet_accounts
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    identity = HostIdentity("c" * 64, str(uuid.uuid4()))
    principal = bot_auth.BotPrincipal.internal(None)
    with Factory() as db:
        package = models.Package(name="Plain purchase", quota_gb=2, duration_days=30, price=100)
        db.add_all([models.PanelSettings(id=1), package])
        db.flush()
        user = wallet_accounts.create_user_with_wallet(db,
            username="existing", telegram_id=881123, balance=0)
        db.commit()
        purchase_intent = ri.ApprovalIntent(pending_source_instance_id="purchase-private", pending_local_id=1,
            kind="new", target_username="existing", amount=100, package_id=package.id,
            claimed_telegram_id=user.telegram_id)
        approval = registration.register(db, principal, purchase_intent, approval_mode="auto")
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == approval.approval_uuid).values(
                registered_under_mode="required", completeness_generation=1))
        db.get(mp.ProvisioningTypeMode, "purchase").mode = "durable"
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
        user_id, package_id = user.id, package.id
    kwargs = dict(identity=identity, ownership_epoch=1, installation_uuid=installation)

    def stage(db):
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            return purchase_zero.prepare(db, approval.approval_uuid, approval.execution_token,
                principal, purchase_intent, **kwargs)

    def finish(db, operation_id, leases, version=0):
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            return purchase_zero.finish(db, approval.approval_uuid, approval.execution_token,
                operation_id, version, leases, principal, purchase_intent, **kwargs)

    with Factory() as db:
        operation_id, leases, replay = stage(db)
        assert not replay
        def fail_commit(_session):
            raise RuntimeError("commit interrupted")
        event.listen(db, "before_commit", fail_commit)
        try:
            try:
                db.commit()
            except RuntimeError:
                pass
            else:
                raise AssertionError("T1 commit unexpectedly succeeded")
        finally:
            event.remove(db, "before_commit", fail_commit)
            db.rollback()
    with Factory() as db:
        assert db.query(mp.ProvisioningOperation).count() == 0
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approval.approval_uuid)).mappings().one()["state"] == "registered"
    with Factory() as db:
        operation_id, leases, replay = stage(db)
        assert not replay
        db.commit()
    with Factory() as db:
        repeated_id, _, replay = stage(db)
        assert replay and repeated_id == operation_id
        db.rollback()
    with Factory() as db:
        db.get(models.Package, package_id).duration_days = 31
        db.commit()
    with Factory() as db:
        try:
            finish(db, operation_id, leases)
        except HTTPException as exc:
            assert exc.status_code == 409 and exc.detail == "provisioning_target_changed"
        else:
            raise AssertionError("package drift was accepted")
        db.rollback()
    with Factory() as db:
        db.get(models.Package, package_id).duration_days = 30
        db.commit()
    with Factory() as db:
        operation = finish(db, operation_id, leases)
        assert operation.state == "completed"
        assert db.query(models.Purchase).count() == 1
        assert db.query(models.LedgerEntry).count() == 1
        assert len(db.execute(rv.receipt_approval_effects.select()).all()) == 2
        def fail_commit(_session):
            raise RuntimeError("commit interrupted")
        event.listen(db, "before_commit", fail_commit)
        try:
            try:
                db.commit()
            except RuntimeError:
                pass
            else:
                raise AssertionError("commit unexpectedly succeeded")
        finally:
            event.remove(db, "before_commit", fail_commit)
            db.rollback()
    with Factory() as db:
        assert db.query(models.Purchase).count() == 0
        assert db.query(models.LedgerEntry).count() == 0
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approval.approval_uuid)).mappings().one()["state"] == "mutating"
    with Factory() as db:
        operation = finish(db, operation_id, leases)
        db.commit()
        result_purchase_id = operation.result_purchase_id
    with Factory() as db:
        repeated_id, _, replay = stage(db)
        assert replay and repeated_id == operation_id
        db.rollback()
    with Factory() as db:
        assert finish(db, operation_id, (), version=1).id == operation_id
        db.rollback()
    with Factory() as db:
        assert db.get(models.Purchase, result_purchase_id).user_id == user_id
        assert db.query(models.Purchase).count() == 1
        assert db.query(models.LedgerEntry).count() == 1
        assert db.get(models.User, user_id).balance == 0
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == approval.approval_uuid)).mappings().one()["state"] == "completed"
    print("PASS", engine.dialect.name, "atomic existing-user purchase")


with tempfile.TemporaryDirectory(prefix="um-p6-approval-purchase-") as directory:
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
