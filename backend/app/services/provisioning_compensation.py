"""DB-only T_comp for non-approval legacy-generation operations.

Caller owns runtime/resource leases and a short writer transaction, rolls
back any failure and commits once. No remote calls, hidden commit or force.
Approval operations and forced-abandoned steps remain deliberately refused.
"""
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import select

from .. import models_provisioning as mp
from . import payment_reservations, provisioning_transitions as transitions


def finish(db, operation_id, version):
    # Canonical wallet runtime/epoch precedes operation and reservation locks.
    operation = payment_reservations._operation(db, operation_id)
    if operation.approval_uuid is not None:
        raise HTTPException(503, "provisioning_approval_integration_unavailable")
    if operation.operation_type.startswith("delete_"):
        raise HTTPException(409, "provisioning_delete_irreversible")
    if operation.state == "compensated":
        return operation
    if operation.version != version:
        raise HTTPException(409, "provisioning_operation_changed")
    if operation.state != "compensating":
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    if operation.error_code not in transitions.PERMANENT_ERRORS:
        raise HTTPException(409, "provisioning_error_code_invalid")
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                       .order_by(mp.ProvisioningStep.id).with_for_update()
                       .execution_options(populate_existing=True)).scalars().all()
    if any(step.state != "removed" or (step.remote_attempted and step.remote_outcome not in (
            "verified_absent", "delete_idempotently_absent")) for step in steps):
        raise HTTPException(409, "provisioning_steps_incomplete")
    reservations = db.execute(select(mp.PaymentReservation).where(
        mp.PaymentReservation.operation_id == operation_id).order_by(mp.PaymentReservation.id)
        .with_for_update().execution_options(populate_existing=True)).scalars().all()
    if any(row.state == "captured" for row in reservations):
        raise HTTPException(409, "payment_reservation_already_captured")
    for reservation in reservations:
        payment_reservations.release(db, reservation.id)
    claims = db.execute(select(mp.OneTimePackageClaim).where(
        mp.OneTimePackageClaim.operation_id == operation_id, mp.OneTimePackageClaim.release_seq == 0)
        .order_by(mp.OneTimePackageClaim.id).with_for_update()
        .execution_options(populate_existing=True)).scalars().all()
    now = dt.datetime.utcnow()
    for claim in claims:
        result = db.execute(mp.OneTimePackageClaim.__table__.update().where(
            mp.OneTimePackageClaim.id == claim.id, mp.OneTimePackageClaim.release_seq == 0
        ).values(release_seq=claim.id, released_at=now))
        if result.rowcount != 1:
            raise HTTPException(409, "provisioning_claim_changed")
        db.expire(claim)
    transitions._write_operation(db, operation, "compensated", username_claim=None, completed_at=now)
    return operation
