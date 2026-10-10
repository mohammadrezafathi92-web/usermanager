"""Private, DB-only compensation for a zero-step receipt approval.

This is deliberately limited to the same no-connection, no-reservation shape
as provisioning_approval_t1. The caller owns each transaction and its commit;
neither function is connected to an API, scheduler, or remote worker.
"""
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import bot_auth, provisioning_schema, provisioning_transitions as transitions
from . import receipt_approval_intent as intent, receipt_approval_runtime as runtime
from . import receipt_void_schema, resource_leases
from .provisioning_host import HostIdentity, validate_snapshot


def _lock(db, approval_uuid, execution_version, operation_id, version, leases,
          principal, *, identity, ownership_epoch, installation_uuid,
          allow_terminal=False):
    if (db.in_transaction() or not isinstance(approval_uuid, str) or not approval_uuid or
            type(execution_version) is not int or execution_version < 0 or
            type(operation_id) is not int or operation_id < 1 or
            type(version) is not int or version < 0 or not isinstance(identity, HostIdentity) or
            not isinstance(principal, bot_auth.BotPrincipal) or
            not intent.is_managed_principal(principal)):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    receipt_void_schema.assert_ready()
    provisioning_schema.assert_ready()
    resource_leases.begin_business(db)
    # The owner-approved order is wallet/receipt runtime, approval, leases,
    # then operation/step and financial rows. Epoch drift must NOT prevent
    # release of an already-started operation.
    wallet = db.execute(select(rv.wallet_runtime_state.c.phase).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).one_or_none()
    if wallet is None:
        raise HTTPException(503, "wallet_phase_unavailable")
    receipt = runtime.read_state(db, lock=True, shared_lock=True)
    if receipt is None or runtime.effective_registration_mode(receipt) == "blocked":
        raise HTTPException(503, "receipt_approval_blocked")
    host = db.execute(select(mp.ProvisioningRuntimeState.__table__).where(
        mp.ProvisioningRuntimeState.id == 1).with_for_update(read=True)).mappings().one_or_none()
    validate_snapshot(host, identity, ownership_epoch, installation_uuid)
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one_or_none()
    if approval is None or approval["version"] != execution_version:
        raise HTTPException(409, "approval_superseded")
    if (approval["kind"], approval["target_shape"], approval["tenant_scope_key"],
            approval["owner_admin_id_snapshot"], approval["registered_under_mode"]) != (
            "new", "new_user", "shared", None, "required"):
        raise HTTPException(409, "approval_t1_shape_not_supported")
    bound_uuid = approval["execution_key_instance_uuid"]
    if principal.is_internal:
        if principal.key_id is not None or bound_uuid is not None:
            raise HTTPException(403, "execution_principal_mismatch")
    else:
        if principal.key_id is None or bound_uuid is None:
            raise HTTPException(403, "execution_principal_mismatch")
        key = db.execute(select(models.ApiKey).where(models.ApiKey.id == principal.key_id)
            .with_for_update()).scalar_one_or_none()
        if (key is None or not key.enabled or key.key_instance_uuid != bound_uuid or
                key.key_type != bot_auth.KeyType.REMOTE_SHARED_BOT):
            raise HTTPException(403, "execution_key_revoked")
    if bot_auth.resolve_claimed_owner(db, principal, None, endpoint="provisioning_approval") is not None:
        raise HTTPException(403, "execution_scope_mismatch")
    def operation_matches(row):
        return (row is not None and row.operation_type == "create_user" and
            row.approval_uuid == approval_uuid and
            row.approval_execution_version == execution_version and
            row.version == version and row.tenant_scope_key == "shared" and
            row.business_key == f"approval:{approval_uuid}:v:{execution_version}" and
            row.approval_key_bound == (bound_uuid is not None) and
            row.execution_key_instance_uuid == bound_uuid and
            row.target_user_id is None)
    if allow_terminal and approval["state"] == "failed":
        # A successful retry makes no mutation and does not need its old lease.
        done = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.id == operation_id).with_for_update(read=True)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if (not operation_matches(done) or done.state != "compensated" or
                done.error_code not in transitions.PERMANENT_ERRORS):
            raise HTTPException(409, "provisioning_operation_changed")
        if db.scalar(select(rv.receipt_approval_effects.c.id).where(
                rv.receipt_approval_effects.c.approval_uuid == approval_uuid).limit(1)) is not None:
            raise HTTPException(409, "approval_has_effects")
        return done, approval
    if leases is None:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    tokens = tuple(leases)
    keys = {token.resource_key for token in tokens}
    if (len(keys) != len(tokens) or f"provisioning_op:{operation_id}" not in keys or
            any(not key.startswith("customer_identity:") and key != f"provisioning_op:{operation_id}"
                for key in keys) or len({token.owner for token in tokens}) != 1 or
            any(not token.owner.startswith(f"op:{operation_id}:") for token in tokens)):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    operation = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.id == operation_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if not operation_matches(operation):
        raise HTTPException(409, "provisioning_operation_changed")
    for table in (mp.ProvisioningStep, mp.PaymentReservation, mp.OneTimePackageClaim):
        if db.scalar(select(table.id).where(table.operation_id == operation_id)
                .limit(1).with_for_update(read=True)) is not None:
            raise HTTPException(409, "approval_t1_shape_not_supported")
    if db.scalar(select(rv.receipt_approval_effects.c.id).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid)
            .limit(1).with_for_update(read=True)) is not None:
        raise HTTPException(409, "approval_has_effects")
    return operation, approval


def begin(db, approval_uuid, execution_version, operation_id, version, leases,
          principal, error_code, *, identity, ownership_epoch, installation_uuid):
    """Record a permanent failure, leaving the approval mutating for T_comp."""
    if error_code not in transitions.PERMANENT_ERRORS:
        raise HTTPException(422, "provisioning_error_code_invalid")
    operation, approval = _lock(db, approval_uuid, execution_version, operation_id,
        version, leases, principal, identity=identity,
        ownership_epoch=ownership_epoch, installation_uuid=installation_uuid)
    if approval["state"] != "mutating" or operation.state != "prepared":
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    transitions._write_operation(db, operation, "compensating", error_code=error_code)
    db.flush()
    return operation


def finish(db, approval_uuid, execution_version, operation_id, version, leases,
           principal, *, identity, ownership_epoch, installation_uuid):
    """Atomically mark the approval failed and the operation compensated."""
    operation, approval = _lock(db, approval_uuid, execution_version, operation_id,
        version, leases, principal, identity=identity,
        ownership_epoch=ownership_epoch, installation_uuid=installation_uuid,
        allow_terminal=True)
    if approval["state"] == "failed":
        return operation
    if (approval["state"] != "mutating" or operation.state != "compensating" or
            operation.error_code not in transitions.PERMANENT_ERRORS):
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "mutating").values(
            state="failed", failed_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")
    transitions._write_operation(db, operation, "compensated", username_claim=None,
        completed_at=dt.datetime.utcnow())
    db.flush()
    return operation
