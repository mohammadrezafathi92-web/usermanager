"""DB-only create/remove step transitions (Lifecycle 6 / 11).

No worker, remote call, commit or mode activation. The executor must own
canonical runtime/resource leases and a short writer transaction, committing
remote_calling BEFORE sending a request to a node. All mutations use CAS.
Approval-bearing operations fail closed until atomic approval integration.
"""
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from . import provisioning_schema
from .adapter_base import AbsentOutcome, ReadResult, ReadState, PresentResult

TERMINAL = ("completed", "compensated")
PERMANENT_ERRORS = frozenset((
    "step_failed_permanent", "forward_deadline_exceeded", "node_unavailable",
    "target_user_missing", "wallet_epoch_changed", "wg_interface_address_conflict",
    "wg_ip_outside_subnet", "final_tx_failed_permanent", "cancelled_by_recovery",
))
SECRETS = ("staged_wg_private_key", "staged_password", "staged_xr_uuid")
SAFE_STEP_FIELDS = ("id", "operation_id", "slot_key", "direction", "backend", "node_id",
                    "protocol", "state", "remote_attempted", "remote_outcome", "connection_id",
                    "attempts", "next_retry_at", "error_code", "version")


def _operation(db, operation_id):
    provisioning_schema.assert_ready()
    row = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.id == operation_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "provisioning_operation_not_found")
    if row.approval_uuid is not None:
        raise HTTPException(503, "provisioning_approval_integration_unavailable")
    return row


def _step(db, step_id, version):
    operation_id = db.execute(select(mp.ProvisioningStep.operation_id).where(
        mp.ProvisioningStep.id == step_id)).scalar_one_or_none()
    if operation_id is None:
        raise HTTPException(404, "provisioning_step_not_found")
    operation = _operation(db, operation_id)
    row = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.id == step_id)
                     .with_for_update().execution_options(populate_existing=True)).scalar_one()
    if row.version != version:
        raise HTTPException(409, "provisioning_step_changed")
    return operation, row


def _write_step(db, row, state, **values):
    if state in ("active", "removed"):
        values.update({name: None for name in SECRETS})
    elif state in ("compensating", "cleanup_required"):
        values.update(staged_wg_private_key=None, staged_password=None)
    result = db.execute(mp.ProvisioningStep.__table__.update().where(
        mp.ProvisioningStep.id == row.id, mp.ProvisioningStep.state == row.state,
        mp.ProvisioningStep.version == row.version,
    ).values(state=state, version=mp.ProvisioningStep.version + 1, updated_at=dt.datetime.utcnow(), **values))
    if result.rowcount != 1:
        raise HTTPException(409, "provisioning_step_changed")
    db.expire(row)
    return row


def _write_operation(db, operation, state, **values):
    result = db.execute(mp.ProvisioningOperation.__table__.update().where(
        mp.ProvisioningOperation.id == operation.id, mp.ProvisioningOperation.version == operation.version,
        mp.ProvisioningOperation.state == operation.state,
    ).values(state=state, version=mp.ProvisioningOperation.version + 1, **values))
    if result.rowcount != 1:
        raise HTTPException(409, "provisioning_operation_changed")
    db.expire(operation)


def begin_remote(db, step_id, version):
    operation, row = _step(db, step_id, version)
    if operation.operation_type.startswith("delete_") or operation.state not in ("prepared", "provisioning") or row.state != "staged" or (
        row.direction != "create" or row.backend == "radius_ppp"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    if operation.forward_deadline <= dt.datetime.utcnow():
        raise HTTPException(409, "provisioning_forward_deadline_exceeded")
    _write_operation(db, operation, "provisioning")
    return _write_step(db, row, "remote_calling", remote_attempted=True,
                       attempts=row.attempts + 1, next_retry_at=None, error_code=None)


def confirm_present(db, step_id, version, result):
    operation, row = _step(db, step_id, version)
    if not isinstance(result, PresentResult) and not (
        isinstance(result, ReadResult) and result.state == ReadState.PRESENT_MATCH):
        raise HTTPException(422, "provisioning_presence_unverified")
    if operation.operation_type.startswith("delete_") or row.direction != "create" or (
            operation.state not in ("prepared", "provisioning") or row.state != "remote_calling"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    return _write_step(db, row, "remote_created", error_code=None, next_retry_at=None)


def recover_read(db, step_id, version, result):
    if not isinstance(result, ReadResult):
        raise HTTPException(422, "provisioning_read_result_invalid")
    operation, row = _step(db, step_id, version)
    if operation.operation_type.startswith("delete_") or row.direction != "create" or (
            operation.state not in ("prepared", "provisioning") or row.state != "remote_calling"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    if result.state == ReadState.PRESENT_MATCH:
        return _write_step(db, row, "remote_created", error_code=None, next_retry_at=None)
    if result.state == ReadState.ABSENT:
        # Do not forget that a previous executor already attempted remote.
        return _write_step(db, row, "staged", error_code=None, next_retry_at=None)
    if result.state == ReadState.PRESENT_CONFLICT:
        _write_operation(db, operation, "cleanup_required", error_code="remote_identity_conflict")
        return _write_step(db, row, "cleanup_required", remote_outcome="unverified", error_code="remote_identity_conflict")
    return _write_step(db, row, "remote_calling", error_code="remote_unreadable",
                       next_retry_at=dt.datetime.utcnow() + dt.timedelta(seconds=30))


def mark_remote_complete(db, operation_id):
    operation = _operation(db, operation_id)
    if operation.state not in ("prepared", "provisioning"):
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                       .with_for_update().execution_options(populate_existing=True)).scalars().all()
    deleting = operation.operation_type.startswith("delete_")
    if deleting:
        incomplete = any(step.direction != "remove" or step.state != "removed" or (
            step.remote_attempted and step.remote_outcome not in (
                "verified_absent", "delete_idempotently_absent", "abandoned")) for step in steps)
    else:
        incomplete = any(step.direction != "create" or step.state != (
            "staged" if step.backend == "radius_ppp" else "remote_created") for step in steps)
    if incomplete:
        raise HTTPException(409, "provisioning_steps_incomplete")
    _write_operation(db, operation, "remote_complete")
    return operation


def begin_compensation(db, operation_id, error_code):
    if error_code not in PERMANENT_ERRORS:
        raise HTTPException(422, "provisioning_error_code_invalid")
    operation = _operation(db, operation_id)
    if operation.operation_type.startswith("delete_"):
        raise HTTPException(409, "provisioning_delete_irreversible")
    if operation.state not in ("prepared", "provisioning", "remote_complete", "cleanup_required", "compensating"):
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    _write_operation(db, operation, "compensating", error_code=error_code)
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                       .order_by(mp.ProvisioningStep.step_order).with_for_update()
                       .execution_options(populate_existing=True)).scalars().all()
    for row in steps:
        if row.state in ("removed", "compensating"):
            continue
        if row.state == "active":
            raise HTTPException(409, "provisioning_step_transition_invalid")
        if (row.direction == "create" and row.state == "staged" and not row.remote_attempted) or row.backend == "radius_ppp":
            _write_step(db, row, "removed", remote_outcome=None)
        else:
            _write_step(db, row, "compensating", remote_outcome=None)
    return operation


def begin_remove(db, step_id, version):
    operation, row = _step(db, step_id, version)
    if not operation.operation_type.startswith("delete_") or operation.state not in (
            "prepared", "provisioning", "cleanup_required") or row.direction != "remove" or row.state not in (
            "staged", "cleanup_required"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    connection = db.execute(select(models.Connection).where(models.Connection.id == row.connection_id)
                            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if connection is None or (connection.user_id, connection.node_id, connection.type.value) != (
            operation.target_user_id, row.node_id, row.protocol):
        raise HTTPException(409, "provisioning_connection_mismatch")
    if connection.enabled:
        raise HTTPException(409, "provisioning_deletion_not_disabled")
    _write_operation(db, operation, "provisioning")
    if row.backend == "radius_ppp":
        return _write_step(db, row, "removed", remote_outcome=None, error_code=None, next_retry_at=None)
    return _write_step(db, row, "remote_calling", remote_attempted=True,
                       attempts=row.attempts + 1, remote_outcome=None, error_code=None, next_retry_at=None)


def confirm_removed(db, step_id, version, result):
    operation, row = _step(db, step_id, version)
    if not isinstance(result, AbsentOutcome):
        raise HTTPException(422, "provisioning_absence_result_invalid")
    if not operation.operation_type.startswith("delete_") or operation.state != "provisioning" or (
            row.state != "remote_calling" or row.direction != "remove"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    if result == AbsentOutcome.UNVERIFIED:
        _write_operation(db, operation, "cleanup_required", error_code="remote_absence_unverified")
        return _write_step(db, row, "cleanup_required", remote_outcome="unverified",
                           error_code="remote_absence_unverified", next_retry_at=None)
    return _write_step(db, row, "removed", remote_outcome=result.value, error_code=None, next_retry_at=None)


def recover_remove(db, step_id, version, result):
    if not isinstance(result, ReadResult):
        raise HTTPException(422, "provisioning_read_result_invalid")
    operation, row = _step(db, step_id, version)
    if not operation.operation_type.startswith("delete_") or operation.state != "provisioning" or (
            row.direction != "remove" or row.state != "remote_calling"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    if result.state == ReadState.ABSENT:
        return _write_step(db, row, "removed", remote_outcome="verified_absent", error_code=None, next_retry_at=None)
    if result.state == ReadState.PRESENT_CONFLICT:
        _write_operation(db, operation, "cleanup_required", error_code="remote_identity_conflict")
        return _write_step(db, row, "cleanup_required", remote_outcome="unverified",
                           error_code="remote_identity_conflict", next_retry_at=None)
    if result.state == ReadState.PRESENT_MATCH:
        # Still needs ensure_absent. Never classify it as a created resource.
        return _write_step(db, row, "remote_calling", error_code=None, next_retry_at=None)
    return _write_step(db, row, "remote_calling", error_code="remote_unreadable",
                       next_retry_at=dt.datetime.utcnow() + dt.timedelta(seconds=30))


def confirm_absent(db, step_id, version, result):
    operation, row = _step(db, step_id, version)
    if not isinstance(result, AbsentOutcome):
        raise HTTPException(422, "provisioning_absence_result_invalid")
    if operation.state != "compensating" or row.state != "compensating":
        raise HTTPException(409, "provisioning_step_transition_invalid")
    if result == AbsentOutcome.UNVERIFIED:
        _write_operation(db, operation, "cleanup_required", error_code="remote_absence_unverified")
        return _write_step(db, row, "cleanup_required", remote_outcome="unverified", error_code="remote_absence_unverified")
    return _write_step(db, row, "removed", remote_outcome=result.value, next_retry_at=None, error_code=None)


def activate(db, step_id, version, connection_id):
    operation, row = _step(db, step_id, version)
    expected = "staged" if row.backend == "radius_ppp" else "remote_created"
    if operation.state != "remote_complete" or row.state != expected or row.direction != "create":
        raise HTTPException(409, "provisioning_step_transition_invalid")
    connection = db.execute(select(models.Connection).where(models.Connection.id == connection_id)
                            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if connection is None or (connection.node_id, connection.type.value) != (row.node_id, row.protocol):
        raise HTTPException(409, "provisioning_connection_mismatch")
    if operation.target_user_id is not None and operation.target_user_id != connection.user_id:
        raise HTTPException(409, "provisioning_connection_mismatch")
    # T_final must copy the protocol's credentials before their staged copy
    # disappears. Compare identity too: an unrelated row must not consume it.
    if row.protocol == "wireguard":
        fields = (("staged_wg_private_key", "wg_private_key"),
                  ("wg_public_key", "wg_public_key"), ("wg_peer_name", "wg_peer_name"),
                  ("wg_client_address", "wg_client_address"))
    elif row.protocol == "xray":
        fields = (("staged_xr_uuid", "xr_uuid"), ("xr_email", "xr_email"), ("flow", "xr_flow"))
    else:
        fields = (("staged_password", "ppp_password"), ("account_username", "ppp_username"))
    if any(getattr(row, source) is not None and getattr(row, source) != getattr(connection, target)
           for source, target in fields):
        raise HTTPException(409, "provisioning_connection_mismatch")
    return _write_step(db, row, "active", connection_id=connection_id)


def public_step(row):
    """Allowlist only; never secrets, raw errors or remote object handles."""
    return {name: getattr(row, name) for name in SAFE_STEP_FIELDS}
