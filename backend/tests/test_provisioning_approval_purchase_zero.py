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
from app.services import provisioning_approval_zero_comp as compensation
from app.services import provisioning_schema, receipt_approval_intent as ri
from app.services import receipt_approval_registration as registration, receipt_void_schema
from app.services import resource_leases, wallet_accounts
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
    with Factory() as db:
        assert all(resource_leases.release(db, token) for token in leases)
        db.commit()
    # A second no-step purchase can be abandoned without a sale or effects.
    with Factory() as db:
        abandoned_intent = ri.ApprovalIntent(pending_source_instance_id="purchase-private", pending_local_id=2,
            kind="new", target_username="existing", amount=100, package_id=package_id,
            claimed_telegram_id=881123)
        abandoned = registration.register(db, principal, abandoned_intent, approval_mode="auto")
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == abandoned.approval_uuid).values(
                registered_under_mode="required", completeness_generation=1))
        db.commit()
    with Factory() as db:
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            abandoned_id, abandoned_leases, replay = purchase_zero.prepare(db,
                abandoned.approval_uuid, abandoned.execution_token, principal, abandoned_intent, **kwargs)
        assert not replay
        db.commit()
    with Factory() as db:
        operation = compensation.begin(db, abandoned.approval_uuid, abandoned.execution_token,
            abandoned_id, 0, abandoned_leases, principal, "final_tx_failed_permanent",
            operation_type="purchase", **kwargs)
        assert operation.state == "compensating"
        db.commit()
    with Factory() as db:
        operation = compensation.finish(db, abandoned.approval_uuid, abandoned.execution_token,
            abandoned_id, 1, abandoned_leases, principal, operation_type="purchase", **kwargs)
        assert operation.state == "compensated"
        def fail_commit(_session):
            raise RuntimeError("commit interrupted")
        event.listen(db, "before_commit", fail_commit)
        try:
            try:
                db.commit()
            except RuntimeError:
                pass
            else:
                raise AssertionError("compensation commit unexpectedly succeeded")
        finally:
            event.remove(db, "before_commit", fail_commit)
            db.rollback()
    with Factory() as db:
        assert db.get(mp.ProvisioningOperation, abandoned_id).state == "compensating"
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == abandoned.approval_uuid)).mappings().one()["state"] == "mutating"
        db.rollback()
    with Factory() as db:
        operation = compensation.finish(db, abandoned.approval_uuid, abandoned.execution_token,
            abandoned_id, 1, abandoned_leases, principal, operation_type="purchase", **kwargs)
        db.commit()
        compensated_version = operation.version
    with Factory() as db:
        assert compensation.finish(db, abandoned.approval_uuid, abandoned.execution_token,
            abandoned_id, compensated_version, (), principal, operation_type="purchase", **kwargs).id == abandoned_id
        db.rollback()
    with Factory() as db:
        assert db.query(models.Purchase).count() == 1
        assert db.query(models.LedgerEntry).count() == 1
        assert db.query(mp.ProvisioningOperation).count() == 2
        assert len(db.execute(rv.receipt_approval_effects.select()).all()) == 2
    # A migrated customer can retain historical User-level counters. Only
    # unlinked Connections make build_purchase_core absorb another Purchase.
    with Factory() as db:
        migrated = wallet_accounts.create_user_with_wallet(db,
            username="migrated", telegram_id=881124, balance=0)
        migrated.total_quota_bytes = 8 * 1024 ** 3
        migrated.expire_at = dt.datetime.utcnow() + dt.timedelta(days=12)
        migrated.reserved_quota_bytes = 2 * 1024 ** 3
        migrated.reserved_duration_days = 14
        old = models.Purchase(user_id=migrated.id, package_id=package_id,
            quota_bytes=8 * 1024 ** 3, expire_at=migrated.expire_at)
        node = models.Node(name="migrated-node", type=models.NodeType.mikrotik)
        db.add_all([old, node])
        db.flush()
        linked = models.Connection(user_id=migrated.id, purchase_id=old.id,
            node_id=node.id, type=models.ConnectionType.l2tp)
        db.add(linked)
        db.flush()
        migrated_id, old_id, linked_id = migrated.id, old.id, linked.id
        historical = (migrated.total_quota_bytes, migrated.expire_at,
            migrated.reserved_quota_bytes, migrated.reserved_duration_days)
        fresh_intent = ri.ApprovalIntent(pending_source_instance_id="purchase-private", pending_local_id=3,
            kind="new", target_username="migrated", amount=100, package_id=package_id,
            claimed_telegram_id=migrated.telegram_id)
        fresh = registration.register(db, principal, fresh_intent, approval_mode="auto")
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == fresh.approval_uuid).values(
                registered_under_mode="required", completeness_generation=1))
        db.commit()
    with Factory() as db:
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            fresh_id, fresh_leases, replay = purchase_zero.prepare(db,
                fresh.approval_uuid, fresh.execution_token, principal, fresh_intent, **kwargs)
        assert not replay
        db.commit()
    with Factory() as db:
        with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
            new_operation = purchase_zero.finish(db, fresh.approval_uuid, fresh.execution_token,
                fresh_id, 0, fresh_leases, principal, fresh_intent, **kwargs)
        db.commit()
        new_id = new_operation.result_purchase_id
    with Factory() as db:
        migrated = db.get(models.User, migrated_id)
        assert (migrated.total_quota_bytes, migrated.expire_at,
            migrated.reserved_quota_bytes, migrated.reserved_duration_days) == historical
        assert {row.id for row in migrated.purchases} == {old_id, new_id}
        assert db.get(models.Connection, linked_id).purchase_id == old_id
        assert len(db.execute(rv.receipt_approval_effects.select().where(
            rv.receipt_approval_effects.c.approval_uuid == fresh.approval_uuid)).all()) == 2
    with Factory() as db:
        unmigrated = wallet_accounts.create_user_with_wallet(db,
            username="unmigrated", telegram_id=881125, balance=0)
        node = db.query(models.Node).filter_by(name="migrated-node").one()
        db.add(models.Connection(user_id=unmigrated.id, node_id=node.id,
            type=models.ConnectionType.l2tp))
        old_intent = ri.ApprovalIntent(pending_source_instance_id="purchase-private", pending_local_id=4,
            kind="new", target_username="unmigrated", amount=100, package_id=package_id,
            claimed_telegram_id=unmigrated.telegram_id)
        old_approval = registration.register(db, principal, old_intent, approval_mode="auto")
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == old_approval.approval_uuid).values(
                registered_under_mode="required", completeness_generation=1))
        db.commit()
    with Factory() as db:
        try:
            with patch.object(binding.runtime, "effective_registration_mode", return_value="required"):
                purchase_zero.prepare(db, old_approval.approval_uuid,
                    old_approval.execution_token, principal, old_intent, **kwargs)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (409, "approval_purchase_shape_not_supported")
        else:
            raise AssertionError("unmigrated connection was accepted")
        db.rollback()
    with Factory() as db:
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == old_approval.approval_uuid)).mappings().one()["state"] == "registered"
        assert db.query(mp.ProvisioningOperation).filter_by(approval_uuid=old_approval.approval_uuid).count() == 0
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
