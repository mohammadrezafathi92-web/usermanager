"""Bounded tick of already-prepared non-approval operations; no live wiring.

Caller owns the complete T1 lease set and its expected installation/owner
epoch. Every tick renews live leases in a short transaction. A child call is
outside ALL parent DB sessions. This coordinator does not acquire/reclaim
ownership, enable modes, scan arbitrary jobs, or run a background scheduler.
Approval integration remains separate. Recovery can reacquire expired lease
sets, but never steals live leases or releases a child-held node gate.
"""
import datetime as dt
import secrets

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import resource_leases as leases, provisioning_schema, wallet_service
from . import provisioning_transitions as transitions, provisioning_finalization as final
from . import provisioning_compensation as compensation, provisioning_parent_execute as execute
from .provisioning_host import HostIdentity

KINDS = ("create_user", "purchase", "add_connection")


def _control(db, operation_id, identity, expected):
    provisioning_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    kind = db.scalar(select(mp.ProvisioningOperation.operation_type).where(mp.ProvisioningOperation.id == operation_id))
    if kind not in KINDS:
        raise HTTPException(409, "provisioning_worker_operation_unavailable")
    mode = db.scalar(select(mp.ProvisioningTypeMode.mode).where(mp.ProvisioningTypeMode.operation_type == kind)
        .with_for_update(read=True))
    runtime = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if mode != "durable" or runtime is None or runtime.gate_mode != "enforced" or runtime.owner_state not in (
            "active", "draining") or (runtime.installation_uuid, runtime.ownership_epoch) != expected or (
            runtime.owner_host_id, runtime.owner_boot_id) != (identity.host_id, identity.boot_id) or (
            runtime.lock_backend != ("flock" if db.get_bind().dialect.name == "sqlite" else "flock+get_lock")):
        raise HTTPException(409, "provisioning_worker_owner_changed")
    panel = db.get(models.PanelSettings, 1, populate_existing=True)
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")


def _public(operation, status, step=None):
    return dict(operation_id=operation.id, state=operation.state, version=operation.version,
        status=status, step=step, result_user_id=operation.result_user_id, result_purchase_id=operation.result_purchase_id)


def _required_keys(db, operation, steps):
    required = {f"provisioning_op:{operation.id}"}
    required.update(f"node:{step.node_id}:wg_pool" for step in steps if step.backend == "mikrotik_wg")
    intent = final._intent(operation)
    if operation.target_user_id is not None:
        identities = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
            rv.wallet_accounts.c.user_id_snapshot == operation.target_user_id,
            rv.wallet_accounts.c.username_snapshot == intent.user.username)).scalars().all()
        if len(set(identities)) != 1:
            raise HTTPException(409, "provisioning_lease_scope_invalid")
        required.add(f"customer_identity:{identities[0]}")
    elif intent.user.telegram_id is not None:
        customer = db.scalar(select(rv.customer_identities.c.id).where(
            rv.customer_identities.c.tenant_scope_key == operation.tenant_scope_key,
            rv.customer_identities.c.identity_key == f"tg:{intent.user.telegram_id}"))
        if customer is not None:
            required.add(f"customer_identity:{customer}")
    return required


def _scope(db, operation, steps, tokens):
    if not _required_keys(db, operation, steps).issubset({token.resource_key for token in tokens}):
        raise HTTPException(409, "provisioning_lease_scope_invalid")


def reacquire(session_factory, operation_id, identity, *, installation_uuid, ownership_epoch):
    """Recover a committed operation's lease set in ONE short transaction.

    Returns no leases for a terminal operation. Busy acquisition rolls back
    the entire set. A new owner nonce and every resource's incremented fencing
    epoch invalidate stale executors; this is NOT proof a remote call ended.
    The next tick therefore retains remote_calling recovery / cleanup policy.
    No T1 rerun, secret generation, payment mutation, remote I/O or activation.
    """
    if type(operation_id) is not int or operation_id < 1 or not isinstance(identity, HostIdentity) or (
            type(ownership_epoch) is not int or ownership_epoch < 1 or not isinstance(installation_uuid, str)):
        raise HTTPException(422, "provisioning_worker_invalid")
    expected = (installation_uuid, ownership_epoch)
    with session_factory() as db:
        leases.begin_business(db)
        _control(db, operation_id, identity, expected)  # L1.
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation is None or operation.approval_uuid is not None:
            raise HTTPException(409, "provisioning_worker_operation_unavailable")
        if operation.state in transitions.TERMINAL:
            return ()
        steps = db.execute(select(mp.ProvisioningStep).where(
            mp.ProvisioningStep.operation_id == operation_id)).scalars().all()
        keys = _required_keys(db, operation, steps)
        # Snapshot only; L4 locking follows acquisition of ALL L2 resources.
        version = operation.version
        shape = sorted((step.id, step.version, step.node_id, step.backend) for step in steps)
        owner = f"op:{operation_id}:{secrets.randbits(64) + 1}"
        tokens = tuple(leases.acquire(db, key, owner, ttl=300) for key in sorted(keys))
        operation = transitions._operation(db, operation_id)  # L4, fresh current read.
        steps = db.execute(select(mp.ProvisioningStep).where(
            mp.ProvisioningStep.operation_id == operation_id).with_for_update()
            .execution_options(populate_existing=True)).scalars().all()
        if operation.version != version or shape != sorted(
                (step.id, step.version, step.node_id, step.backend) for step in steps) or (
                keys != _required_keys(db, operation, steps)):
            raise HTTPException(409, "provisioning_operation_changed")
        leases.revalidate(db, tokens)
        db.commit()
        return tokens


def tick(session_factory, operation_id, identity, tokens, *, installation_uuid, ownership_epoch):
    """Advance one remote step OR one atomic final/compensation transaction.

    Unknown results wait for recovery; identity conflict requires explicit
    recovery policy. Neither is interpreted as absence or a reason to refund.
    Leases remain caller-owned even on terminal replay; caller releases them
    only after observing a committed terminal result.
    """
    if type(operation_id) is not int or operation_id < 1 or not isinstance(identity, HostIdentity) or (
            type(ownership_epoch) is not int or ownership_epoch < 1 or not isinstance(installation_uuid, str)):
        raise HTTPException(422, "provisioning_worker_invalid")
    tokens = tuple(tokens)
    expected = (installation_uuid, ownership_epoch)
    with session_factory() as db:
        leases.begin_business(db)
        _control(db, operation_id, identity, expected)  # L1 before any L2/L4 locking.
        leases.revalidate(db, tokens)
        if len({token.owner for token in tokens}) != 1 or not any(
                token.resource_key == f"provisioning_op:{operation_id}" for token in tokens) or any(
                not token.owner.startswith(f"op:{operation_id}:") for token in tokens):
            raise HTTPException(409, "provisioning_lease_scope_invalid")
        for token in tokens:
            leases.renew(db, token, ttl=300)  # > the child hard deadline (120s), not an unbounded heartbeat.
        operation = transitions._operation(db, operation_id)
        if operation.state in transitions.TERMINAL:
            result = _public(operation, "terminal")
            db.commit()
            return result
        steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
            .order_by(mp.ProvisioningStep.step_order, mp.ProvisioningStep.id).with_for_update()
            .execution_options(populate_existing=True)).scalars().all()
        _scope(db, operation, steps, tokens)
        clock, _ = leases._clock(db)
        now = db.scalar(select(clock))
        if isinstance(now, str):
            now = dt.datetime.fromisoformat(now)
        if operation.state in ("prepared", "provisioning", "remote_complete") and operation.forward_deadline <= now:
            transitions.begin_compensation(db, operation.id, "forward_deadline_exceeded")
            result = _public(operation, "compensation_started")
            db.commit()
            return result
        if operation.state == "cleanup_required":
            result = _public(operation, "manual_recovery_required")
            db.commit()
            return result
        cleaning = operation.state == "compensating"
        if cleaning:
            pending = [step for step in steps if step.state != "removed"]
            if not pending:
                compensation.finish(db, operation.id, operation.version)
                result = _public(operation, "compensated")
                db.commit()
                return result
        elif operation.state in ("prepared", "provisioning", "remote_complete"):
            pending = [step for step in steps if step.backend != "radius_ppp" and step.state != "remote_created"]
            pending.sort(key=lambda step: (step.state != "remote_calling", step.step_order, step.id))
            if not pending:
                if operation.state != "remote_complete":
                    transitions.mark_remote_complete(db, operation.id)
                final.finish(db, operation.id, operation.version, tokens, require_contracts=True)
                result = _public(operation, "completed")
                db.commit()
                return result
        else:
            raise HTTPException(409, "provisioning_worker_state_invalid")
        step = pending[0]
        if step.state not in (("compensating",) if cleaning else ("staged", "remote_calling")):
            raise HTTPException(409, "provisioning_worker_state_invalid")
        if step.next_retry_at is not None and step.next_retry_at > now:
            result = _public(operation, "waiting", transitions.public_step(step))
            db.commit()
            return result
        sid, version, recovery_read = step.id, step.version, step.state == "remote_calling"
        db.commit()
    # execute owns its two short transactions; no session above survives.
    if cleaning:
        step_result = execute.execute_compensation(session_factory, sid, version, identity, tokens, expected_owner=expected)
    else:
        step_result = execute.execute_one(session_factory, sid, version, identity, tokens,
            recovery_read=recovery_read, expected_owner=expected)
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation is None:
            raise HTTPException(409, "provisioning_operation_changed")
        return _public(operation, "step_recorded", step_result)


def resume_one(session_factory, operation_id, identity, *, installation_uuid, ownership_epoch):
    """One bounded recovery iteration, with caller-supplied trusted ownership.

    No job scanning, scheduler registration or automatic ownership takeover.
    Live leases return busy. Every remote result is committed (or the short
    transaction rolled back) before releasing this iteration's DB leases.
    Release never touches a child node gate and cannot release a newer owner.
    Unknown writes retain the committed remote_calling marker for next time.
    """
    expected = dict(installation_uuid=installation_uuid, ownership_epoch=ownership_epoch)
    try:
        tokens = reacquire(session_factory, operation_id, identity, **expected)
    except leases.LeaseBusy:
        return dict(operation_id=operation_id, status="lease_busy")
    if not tokens:
        with session_factory() as db:
            operation = db.get(mp.ProvisioningOperation, operation_id)
            if operation is None or operation.state not in transitions.TERMINAL:
                raise HTTPException(409, "provisioning_operation_changed")
            return _public(operation, "terminal")
    try:
        return tick(session_factory, operation_id, identity, tokens, **expected)
    finally:
        # The tick's context managers close/roll back its business sessions
        # before reaching here, including launcher or final commit failures.
        with session_factory() as db:
            for token in tokens:
                leases.release(db, token)
            db.commit()
