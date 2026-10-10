"""Read-only bounded selection of non-approval deletion work.

An ID is a hint, never a claim. The selection session closes before the
private worker reacquires all current leases and before any child is started.
No scheduler, live route, automatic force or mode activation imports this.
"""
from fastapi import HTTPException
from sqlalchemy import String, and_, cast, literal, or_, select

from .. import models_provisioning as mp, models_receipt_void as rv
from . import provisioning_schema, resource_leases as leases, wallet_service
from . import provisioning_operation_worker as create_worker
from . import provisioning_deletion_worker as worker
from .provisioning_host import HostIdentity

MAX_PAGE_SIZE = 25  # Page bound, not a retry cap.
STATES = ("prepared", "provisioning", "remote_complete")


def select_due(session_factory, identity, *, installation_uuid, ownership_epoch, limit=10, after_id=0):
    """Return due operation IDs with a free/expired OP lease, in ID order."""
    if not isinstance(identity, HostIdentity) or type(installation_uuid) is not str or (
            type(ownership_epoch) is not int or ownership_epoch < 1 or type(limit) is not int or
            not 1 <= limit <= MAX_PAGE_SIZE or type(after_id) is not int or after_id < 0):
        raise HTTPException(422, "provisioning_worker_invalid")
    with session_factory() as db:
        provisioning_schema.assert_ready()
        wallet_service.require_legacy_phase(db)
        kinds = tuple(db.execute(select(mp.ProvisioningTypeMode.operation_type).where(
            mp.ProvisioningTypeMode.operation_type.in_(worker.KINDS),
            mp.ProvisioningTypeMode.mode == "durable")
            .order_by(mp.ProvisioningTypeMode.operation_type).with_for_update(read=True)).scalars())
        create_worker._runtime_control(db, identity, (installation_uuid, ownership_epoch))
        now, _ = leases._clock(db)
        op, step, locks = mp.ProvisioningOperation, mp.ProvisioningStep, rv.resource_locks
        # Only the first unfinished step can advance. Do not select a later
        # step merely because its retry clock is earlier.
        retry = select(step.next_retry_at).where(step.operation_id == op.id,
            step.state != "removed").order_by(step.step_order, step.id).limit(1).correlate(op).scalar_subquery()
        live_operation_lease = select(locks.c.resource_key).where(
            locks.c.resource_key == literal("provisioning_op:") + cast(op.id, String),
            locks.c.lease_owner.is_not(None),
            or_(locks.c.leased_until.is_(None), locks.c.leased_until >= now)).correlate(op).exists()
        due = and_(or_(op.next_retry_at.is_(None), op.next_retry_at <= now),
            or_(retry.is_(None), retry <= now))
        ids = tuple(db.execute(select(op.id).where(op.id > after_id, op.approval_uuid.is_(None),
            op.operation_type.in_(kinds), op.state.in_(STATES), ~live_operation_lease, due)
            .order_by(op.id).limit(limit)).scalars())
    return ids


def recover_due_once(session_factory, identity, *, installation_uuid, ownership_epoch, after_id=0):
    """Attempt one selected operation; never spin on a lost lease race."""
    expected = dict(installation_uuid=installation_uuid, ownership_epoch=ownership_epoch)
    ids = select_due(session_factory, identity, limit=1, after_id=after_id, **expected)
    if not ids:
        return dict(status="idle", operation_id=None, selection_cursor=after_id)
    result = worker.resume_one(session_factory, ids[0], identity, **expected)
    return dict(result, selection_cursor=ids[0])
