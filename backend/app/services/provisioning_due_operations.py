"""Private read-only recovery selection, not a scheduler or lease claimant.

Return a bounded page of IDs, never operation intent or credentials. The
session is closed before returning. IDs are only hints: resume_one must still
revalidate current ownership, complete resource scope, versions and readiness
in its own transaction. No live service imports or calls this module.
"""
from fastapi import HTTPException
from sqlalchemy import String, and_, case, cast, literal, or_, select

from .. import models_provisioning as mp, models_receipt_void as rv
from . import provisioning_operation_worker as worker, provisioning_schema
from . import resource_leases as leases, wallet_service
from .provisioning_host import HostIdentity

MAX_PAGE_SIZE = 25  # Selection bound only; NOT a retry cap or execution budget.
FORWARD = ("prepared", "provisioning", "remote_complete")


def select_due(session_factory, identity, *, installation_uuid, ownership_epoch, limit=10, after_id=0):
    """Read the next page using DB time; no writes, lease acquisition or I/O.

    Ascending ID cursor lets a caller visit later work rather than repeatedly
    inspecting an old operation blocked on a shared customer/IP lease. Wrap
    after exhausting a pass. This function never promises it claimed a job.
    An expired forward deadline bypasses backoff to start compensation, but
    compensation itself ALWAYS respects its step/operation retry time.
    """
    if not isinstance(identity, HostIdentity) or not isinstance(installation_uuid, str) or (
            type(ownership_epoch) is not int or ownership_epoch < 1 or type(limit) is not int or
            not 1 <= limit <= MAX_PAGE_SIZE or type(after_id) is not int or after_id < 0):
        raise HTTPException(422, "provisioning_worker_invalid")
    with session_factory() as db:
        provisioning_schema.assert_ready()
        wallet_service.require_legacy_phase(db)
        kinds = tuple(db.execute(select(mp.ProvisioningTypeMode.operation_type).where(
            mp.ProvisioningTypeMode.operation_type.in_(worker.KINDS), mp.ProvisioningTypeMode.mode == "durable")
            .order_by(mp.ProvisioningTypeMode.operation_type).with_for_update(read=True)).scalars())
        worker._runtime_control(db, identity, (installation_uuid, ownership_epoch))
        now, _ = leases._clock(db)
        op, step, locks = mp.ProvisioningOperation, mp.ProvisioningStep, rv.resource_locks
        cleaning = op.state == "compensating"
        pending = or_(and_(cleaning, step.state != "removed"), and_(op.state.in_(FORWARD),
            step.backend != "radius_ppp", step.state != "remote_created"))
        # Same next-step ordering as tick: uncertain forward calls first;
        # cleanup in slot order. A later due slot cannot skip the first one.
        retry = select(step.next_retry_at).where(step.operation_id == op.id, pending).order_by(
            case((and_(op.state.in_(FORWARD), step.state == "remote_calling"), 0), else_=1),
            step.step_order, step.id).limit(1).correlate(op).scalar_subquery()
        live_operation_lease = select(locks.c.resource_key).where(
            locks.c.resource_key == literal("provisioning_op:") + cast(op.id, String),
            locks.c.lease_owner.is_not(None),
            # NULL deadline is malformed, not proof an owned lease expired.
            or_(locks.c.leased_until.is_(None), locks.c.leased_until >= now)).correlate(op).exists()
        ready_time = and_(or_(op.next_retry_at.is_(None), op.next_retry_at <= now),
            or_(retry.is_(None), retry <= now))
        due = or_(ready_time, and_(op.state.in_(FORWARD), op.forward_deadline <= now))
        ids = tuple(db.execute(select(op.id).where(op.id > after_id, op.approval_uuid.is_(None),
            op.operation_type.in_(kinds), op.state.in_(FORWARD + ("compensating",)),
            ~live_operation_lease, due).order_by(op.id).limit(limit)).scalars())
        # Context manager rolls back the read transaction and releases L1
        # shared locks. Do not hand a live Session/ORM object to the executor.
    return ids
