"""Private approval live preflight: frozen manifest and target, no writes/network."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import bot_auth, provisioning_approval_binding as binding
from app.services import provisioning_approval_live as live, provisioning_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_schema, resource_leases


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    principal = bot_auth.BotPrincipal.internal(None)
    with Factory() as db:
        db.add_all([models.PanelSettings(id=1), models.Package(
            name="P", quota_gb=1, duration_days=30, price=100),
            models.User(username="existing", telegram_id=123),
            models.Node(name="test-node", type=models.NodeType.xray, enabled=True)])
        db.commit()
        package = db.query(models.Package).one()
        new_intent = ri.ApprovalIntent(pending_source_instance_id="live", pending_local_id=1,
            kind="new", target_username="new", amount=100, package_id=package.id,
            claimed_telegram_id=456)
        existing_intent = ri.ApprovalIntent(pending_source_instance_id="live", pending_local_id=2,
            kind="new", target_username="existing", amount=100, package_id=package.id)
        node = db.query(models.Node).one()
        connected_intent = ri.ApprovalIntent(pending_source_instance_id="live", pending_local_id=3,
            kind="new", target_username="connected", amount=100, package_id=package.id,
            claimed_telegram_id=789,
            connections=(ri.ConnectionSpec(node_id=node.id, protocol="xray"),))
        renew_intent = ri.ApprovalIntent(pending_source_instance_id="live", pending_local_id=4,
            kind="renew", target_username="existing", amount=100, package_id=package.id)
        new_approval = registration.register(db, principal, new_intent, approval_mode="auto")
        existing_approval = registration.register(db, principal, existing_intent, approval_mode="auto")
        connected_approval = registration.register(db, principal, connected_intent, approval_mode="auto")
        renew_approval = registration.register(db, principal, renew_intent, approval_mode="auto")
        for kind in ("create_user", "purchase", "renew_user"):
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        package_id = package.id
        node_id = node.id

    def check(approval, execution_intent, kind, mismatch):
        with Factory() as db:
            resource_leases.begin_business(db)
            locked = binding.lock_prepare(db, approval.approval_uuid, approval.execution_token,
                principal, kind, execution_intent=execution_intent)
            lease = resource_leases.acquire(db, "provisioning_op:1", "op:1:1")
            result = live.compare(db, locked, execution_intent, (lease,))
            assert result.mismatch == mismatch, (result, mismatch)
            assert bool(result.manifest_rows) == (mismatch is None)
            db.rollback()

    check(new_approval, new_intent, "create_user", None)
    check(existing_approval, existing_intent, "purchase", None)
    check(connected_approval, connected_intent, "create_user", None)
    check(renew_approval, renew_intent, "renew_user", "target_shape_unverifiable")
    with Factory() as db:
        resource_leases.begin_business(db)
        locked = binding.lock_prepare(db, new_approval.approval_uuid, new_approval.execution_token,
            principal, "create_user", execution_intent=new_intent)
        try:
            live.compare(db, locked, new_intent, ())
        except resource_leases.LeaseProtocolError as exc:
            assert str(exc) == "invalid_lease_set"
        else:
            raise AssertionError("live preflight accepted no lease")
        db.rollback()
    with Factory() as db:
        db.get(models.Node, node_id).enabled = False
        db.commit()
    check(connected_approval, connected_intent, "create_user", "manifest_precondition_failed")
    with Factory() as db:
        db.get(models.Node, node_id).enabled = True
        db.commit()
    with Factory() as db:
        bundled = models.PackageConnection(package_id=package_id, node_id=node_id,
            protocol=models.ConnectionType.xray)
        db.add(bundled)
        db.commit()
        bundled_id = bundled.id
    check(new_approval, new_intent, "create_user", "manifest_precondition_failed")
    check(existing_approval, existing_intent, "purchase", "manifest_precondition_failed")
    check(connected_approval, connected_intent, "create_user", None)
    with Factory() as db:
        db.delete(db.get(models.PackageConnection, bundled_id))
        db.commit()
    with Factory() as db:
        db.get(models.Package, package_id).quota_gb = 2
        db.commit()
    check(new_approval, new_intent, "create_user", "manifest_precondition_failed")
    check(existing_approval, existing_intent, "purchase", "manifest_precondition_failed")
    with Factory() as db:
        db.get(models.Package, package_id).quota_gb = 1
        db.get(models.User, 1).username = "renamed"
        db.commit()
    check(existing_approval, existing_intent, "purchase", "target_user_changed")
    with Factory() as db:
        db.add(models.User(username="new", telegram_id=999))
        db.commit()
    check(new_approval, new_intent, "create_user", "username_taken")
    with Factory() as db:
        assert db.execute(rv.receipt_approvals.select().where(
            rv.receipt_approvals.c.approval_uuid == new_approval.approval_uuid)).mappings().one()["state"] == "registered"
        assert db.query(mp.ProvisioningOperation).count() == 0
        assert db.query(mp.ProvisioningStep).count() == 0
    print("PASS", engine.dialect.name, "approval live target/manifest preflight")


with tempfile.TemporaryDirectory(prefix="um-p6-approval-live-") as directory:
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
