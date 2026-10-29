"""Explicit, audited recovery for a manually removed Xray client.

No network is used. The admin action may abandon only an unknown remote
delete result after the operator attests that they removed the client by
hand; it must never call the node or mislabel the outcome verified.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-manual-abandon-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, models_receipt_void
from app.database import SessionLocal, engine
from app.services import provisioning_operator as operator, provisioning_schema, receipt_void_schema
from app.services import remote_runner, resource_leases
from app.services.wallet_accounts import create_user_with_wallet

models.Base.metadata.create_all(engine)
assert receipt_void_schema.bootstrap(engine, SessionLocal)["ready"]
assert provisioning_schema.bootstrap(engine, SessionLocal)["ready"]

with SessionLocal() as db:
    admin = models.AdminUser(username="root", hashed_password="test", is_superadmin=True)
    user = create_user_with_wallet(db, username="manual-xray-delete", balance=0)
    node = models.Node(name="3xui", type=models.NodeType.xray, enabled=True,
                       xr_panel_mode="3x-ui", xr_panel_inbound_id=1, xr_panel_base_url="https://example.invalid")
    db.add_all([admin, node])
    db.flush()
    purchase = models.Purchase(user_id=user.id, quota_bytes=1000)
    db.add(purchase)
    db.flush()
    connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
        type=models.ConnectionType.xray, enabled=False, xr_email="manual-xray-delete@example.invalid",
        xr_uuid="non-secret-test-uuid")
    db.add(connection)
    db.flush()
    operation = mp.ProvisioningOperation(operation_type="delete_purchase", business_key="manual-xray-delete",
        request_hash="a" * 64, tenant_scope_key="shared", actor_kind="admin", actor_id=admin.id,
        intent="{}", target_user_id=user.id, state="provisioning",
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
    db.add(operation)
    db.flush()
    step = mp.ProvisioningStep(operation_id=operation.id, slot_key=f"connection:{connection.id}",
        direction="remove", step_order=0, backend="threexui", protocol="xray", node_id=node.id,
        connection_id=connection.id, xr_email=connection.xr_email, xr_panel_inbound_id=1,
        state="remote_calling", remote_attempted=True, error_code="remote_result_unknown")
    db.add(step)
    db.commit()
    OPERATION_ID, STEP_ID, ADMIN_ID, CONNECTION_ID = operation.id, step.id, admin.id, connection.id

CONFIRM = operator.MANUAL_XRAY_ABANDON_CONFIRMATION
def refused(code, call):
    try:
        call()
        raise AssertionError("unexpected acceptance")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)

with patch.object(remote_runner, "live_count", return_value=1):
    refused("remote_runner_alive", lambda: operator.abandon_unknown_xray_delete(
        SessionLocal, ADMIN_ID, OPERATION_ID, STEP_ID, "reason", CONFIRM, True))
with patch.object(remote_runner, "live_count", return_value=0):
    result = operator.abandon_unknown_xray_delete(SessionLocal, ADMIN_ID, OPERATION_ID, STEP_ID,
        "Removed manually in 3X-UI; accept the unverified result", CONFIRM, True)
assert result == {"operation_id": OPERATION_ID, "step_id": STEP_ID, "abandoned": True}
with patch.object(remote_runner, "live_count", return_value=1):
    assert operator.abandon_unknown_xray_delete(SessionLocal, ADMIN_ID, OPERATION_ID, STEP_ID,
        "retry after response loss", CONFIRM, True) == result

with SessionLocal() as db:
    operation = db.get(mp.ProvisioningOperation, OPERATION_ID)
    step = db.get(mp.ProvisioningStep, STEP_ID)
    assert operation.state == "provisioning" and operation.next_retry_at is None
    assert step.state == "removed" and step.remote_outcome == "abandoned"
    assert step.forced_by_admin_id == ADMIN_ID
    assert step.force_reason == "Removed manually in 3X-UI; accept the unverified result"
    assert step.forced_at is not None
    assert step.remote_outcome != "verified_absent"
    assert db.get(models.Connection, CONNECTION_ID) is not None

with patch.object(remote_runner, "live_count", return_value=0):
    refused("manual_xray_abandon_confirmation_required", lambda: operator.abandon_unknown_xray_delete(
        SessionLocal, ADMIN_ID, OPERATION_ID, STEP_ID, "reason", CONFIRM, False))
with SessionLocal() as db:
    resource_leases.begin_business(db)
    active = resource_leases.acquire(db, f"provisioning_op:{OPERATION_ID}", f"op:{OPERATION_ID}:987654321")
    db.commit()
with patch.object(remote_runner, "live_count", return_value=0):
    refused("provisioning_operation_busy", lambda: operator.abandon_unknown_xray_delete(
        SessionLocal, ADMIN_ID, OPERATION_ID, STEP_ID, "reason", CONFIRM, True))
with SessionLocal() as db:
    resource_leases.begin_business(db)
    assert resource_leases.release(db, active)
    db.commit()

with SessionLocal() as db:
    assert db.get(mp.ProvisioningStep, STEP_ID).remote_outcome == "abandoned"

# The bounded lease created by the safe updater can be adopted only by this
# exact recovery action; the normal active lease above was correctly refused.
with SessionLocal() as db:
    resource_leases.begin_business(db)
    deployment_operation = mp.ProvisioningOperation(
        operation_type="delete_purchase", business_key="deployment-hold-recovery",
        request_hash="b" * 64, tenant_scope_key="shared", actor_kind="admin", actor_id=ADMIN_ID,
        intent="{}", target_user_id=1, state="provisioning",
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0,
    )
    db.add(deployment_operation)
    db.flush()
    deployment_step = mp.ProvisioningStep(
        operation_id=deployment_operation.id, slot_key="connection:deployment-hold",
        direction="remove", step_order=0, backend="threexui", protocol="xray", node_id=1,
        connection_id=CONNECTION_ID, xr_email="deployment-hold@example.invalid", xr_panel_inbound_id=1,
        state="remote_calling", remote_attempted=True, error_code="remote_result_unknown",
    )
    db.add(deployment_step)
    db.flush()
    deployment_operation_id, deployment_step_id = deployment_operation.id, deployment_step.id
    deployment_hold = resource_leases.acquire(
        db, f"provisioning_op:{deployment_operation_id}",
        f"op:{deployment_operation_id}:{models_receipt_void.DEPLOYMENT_HOLD_NONCE}", ttl=3600,
    )
    db.commit()

with patch.object(remote_runner, "live_count", return_value=0):
    adopted = operator.abandon_unknown_xray_delete(
        SessionLocal, ADMIN_ID, deployment_operation_id, deployment_step_id,
        "Removed manually in 3X-UI; accept the unverified result", CONFIRM, True,
    )
assert adopted == {"operation_id": deployment_operation_id, "step_id": deployment_step_id, "abandoned": True}
with SessionLocal() as db:
    adopted_step = db.get(mp.ProvisioningStep, deployment_step_id)
    assert adopted_step.state == "removed" and adopted_step.remote_outcome == "abandoned"
    assert adopted_step.forced_by_admin_id == ADMIN_ID and adopted_step.forced_at is not None
    assert adopted_step.remote_outcome != "verified_absent"
    lock = db.execute(resource_leases.rv.resource_locks.select().where(
        resource_leases.rv.resource_locks.c.resource_key == f"provisioning_op:{deployment_operation_id}"
    )).mappings().one()
    assert lock["lease_owner"] is None
    assert lock["fencing_epoch"] > deployment_hold.epoch
assert _no_network.attempts == []
engine.dispose()
print("all checks passed")
