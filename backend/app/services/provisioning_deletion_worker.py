"""Private bounded coordinator for prepared non-approval deletions.

No API, scheduler, mode activation, remote-node discovery or implicit force.
T1 and T_final each commit once; a remote child runs outside parent sessions.
An uncertain result retains its attempted marker, while an identity conflict
requires an explicit recovery decision and never deletes a DB record.
"""
import datetime as dt
import secrets

from fastapi import HTTPException
from sqlalchemy import select

from .. import models_provisioning as mp, models_receipt_void as rv
from . import provisioning_schema, wallet_service, resource_leases as leases
from . import provisioning_delete_preparation as preparation
from . import provisioning_deletion as deletion, provisioning_transitions as transitions
from . import provisioning_parent_execute as execute
from . import provisioning_operation_worker as create_worker
from .provisioning_host import HostIdentity

KINDS = ("delete_connection", "delete_purchase", "delete_user")


def _control(db, operation_id, identity, expected):
    provisioning_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    kind = db.scalar(select(mp.ProvisioningOperation.operation_type).where(
        mp.ProvisioningOperation.id == operation_id))
    if kind not in KINDS:
        raise HTTPException(409, "provisioning_worker_operation_unavailable")
    if db.scalar(select(mp.ProvisioningTypeMode.mode).where(
            mp.ProvisioningTypeMode.operation_type == kind).with_for_update(read=True)) != "durable":
        raise HTTPException(409, "provisioning_worker_owner_changed")
    return create_worker._runtime_control(db, identity, expected)


def _required_keys(db, operation):
    identities = tuple(db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
        rv.wallet_accounts.c.user_id == operation.target_user_id,
        rv.wallet_accounts.c.tombstoned_at.is_(None))).scalars())
    if len(identities) != 1:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    return {f"customer_identity:{identities[0]}", f"provisioning_op:{operation.id}"}


def _public(operation, status, step=None):
    return dict(operation_id=operation.id, state=operation.state, version=operation.version,
        status=status, step=step)


def reacquire(session_factory, operation_id, identity, *, installation_uuid, ownership_epoch):
    """Reclaim only an expired/free complete set; never steal a live lease."""
    if type(operation_id) is not int or operation_id < 1 or not isinstance(identity, HostIdentity) or (
            type(installation_uuid) is not str or type(ownership_epoch) is not int or ownership_epoch < 1):
        raise HTTPException(422, "provisioning_worker_invalid")
    expected = (installation_uuid, ownership_epoch)
    with session_factory() as db:
        leases.begin_business(db)
        _control(db, operation_id, identity, expected)
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation is None or operation.approval_uuid is not None:
            raise HTTPException(409, "provisioning_worker_operation_unavailable")
        if operation.state in transitions.TERMINAL:
            return ()
        keys, version = _required_keys(db, operation), operation.version
        steps = tuple(db.execute(select(mp.ProvisioningStep.id, mp.ProvisioningStep.version).where(
            mp.ProvisioningStep.operation_id == operation_id).order_by(mp.ProvisioningStep.id)).all())
        owner = f"op:{operation_id}:{secrets.randbits(64) + 1}"
        tokens = tuple(leases.acquire(db, key, owner, ttl=300) for key in sorted(keys))
        operation = transitions._operation(db, operation_id)
        locked_steps = tuple(db.execute(select(mp.ProvisioningStep.id, mp.ProvisioningStep.version).where(
            mp.ProvisioningStep.operation_id == operation_id).order_by(mp.ProvisioningStep.id)
            .with_for_update()).all())
        if operation.version != version or steps != locked_steps or keys != _required_keys(db, operation):
            raise HTTPException(409, "provisioning_operation_changed")
        leases.revalidate(db, tokens)
        db.commit()
        return tokens


def tick(session_factory, operation_id, identity, tokens, *, installation_uuid, ownership_epoch,
         release_shared_for_remote=False):
    """Advance one DB-only step, one absence result, or atomic T_final."""
    if type(operation_id) is not int or operation_id < 1 or not isinstance(identity, HostIdentity) or (
            type(installation_uuid) is not str or type(ownership_epoch) is not int or ownership_epoch < 1 or
            type(release_shared_for_remote) is not bool):
        raise HTTPException(422, "provisioning_worker_invalid")
    tokens = tuple(tokens)
    expected = (installation_uuid, ownership_epoch)
    with session_factory() as db:
        leases.begin_business(db)
        runtime = _control(db, operation_id, identity, expected)
        leases.revalidate(db, tokens)
        if len({token.owner for token in tokens}) != 1 or any(
                not token.owner.startswith(f"op:{operation_id}:") for token in tokens) or not any(
                token.resource_key == f"provisioning_op:{operation_id}" for token in tokens):
            raise HTTPException(409, "provisioning_lease_scope_invalid")
        for token in tokens:
            leases.renew(db, token, ttl=300)
        operation = transitions._operation(db, operation_id)
        if operation.state in transitions.TERMINAL:
            result = _public(operation, "terminal")
            db.commit()
            return result
        if operation.state not in ("prepared", "provisioning", "remote_complete", "cleanup_required") or (
                {token.resource_key for token in tokens} != _required_keys(db, operation)):
            raise HTTPException(409, "provisioning_worker_state_invalid")
        steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
            .order_by(mp.ProvisioningStep.step_order, mp.ProvisioningStep.id).with_for_update()
            .execution_options(populate_existing=True)).scalars().all()
        clock, _ = leases._clock(db)
        now = db.scalar(select(clock))
        if isinstance(now, str):
            now = dt.datetime.fromisoformat(now)
        if operation.state == "cleanup_required":
            result = _public(operation, "manual_recovery_required")
            db.commit()
            return result
        if operation.next_retry_at is not None and operation.next_retry_at > now:
            result = _public(operation, "waiting")
            db.commit()
            return result
        pending = [step for step in steps if step.state != "removed"]
        if not pending:
            if operation.state != "remote_complete":
                transitions.mark_remote_complete(db, operation_id)
            deletion.finish(db, operation_id, operation.version, tokens)
            result = _public(operation, "completed")
            db.commit()
            return result
        if operation.state == "remote_complete":
            raise HTTPException(409, "provisioning_worker_state_invalid")
        step = pending[0]
        if step.state not in ("staged", "remote_calling"):
            raise HTTPException(409, "provisioning_worker_state_invalid")
        if step.next_retry_at is not None and step.next_retry_at > now:
            result = _public(operation, "waiting", transitions.public_step(step))
            db.commit()
            return result
        if step.backend == "radius_ppp":
            if step.state != "staged":
                raise HTTPException(409, "provisioning_worker_state_invalid")
            transitions.begin_remove(db, step.id, step.version)
            result = _public(operation, "step_recorded", transitions.public_step(step))
            db.commit()
            return result
        if runtime.owner_state == "draining":
            result = _public(operation, "draining", transitions.public_step(step))
            db.commit()
            return result
        sid, version = step.id, step.version
        db.commit()
    if release_shared_for_remote:
        with session_factory() as db:
            leases.begin_business(db)
            _control(db, operation_id, identity, expected)
            leases.revalidate(db, tokens)
            for token in tokens:
                if token.resource_key != f"provisioning_op:{operation_id}" and not leases.release(db, token):
                    raise leases.LeaseLost("resource_lease_lost")
            db.commit()
        tokens = tuple(token for token in tokens if token.resource_key == f"provisioning_op:{operation_id}")
    step_result = execute.execute_removal(session_factory, sid, version, identity, tokens, expected_owner=expected)
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation is None:
            raise HTTPException(409, "provisioning_operation_changed")
        return _public(operation, "step_recorded", step_result)


def resume_one(session_factory, operation_id, identity, *, installation_uuid, ownership_epoch):
    """One bounded iteration; the caller decides when/if to call again."""
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
        return tick(session_factory, operation_id, identity, tokens, release_shared_for_remote=True, **expected)
    finally:
        with session_factory() as db:
            for token in tokens:
                leases.release(db, token)
            db.commit()


def start_once(session_factory, request, identity, *, installation_uuid, ownership_epoch,
               actor_kind="system", actor_id=None):
    """Trusted private caller only: commit T1 then advance one iteration."""
    if not isinstance(request, preparation.DeleteRequest):
        raise HTTPException(422, "provisioning_worker_invalid")
    expected = dict(installation_uuid=installation_uuid, ownership_epoch=ownership_epoch)
    with session_factory() as db:
        execute._configured_source(db.get_bind().url)
        leases.begin_business(db)
        prepared = preparation.prepare(db, request, identity=identity, actor_kind=actor_kind,
            actor_id=actor_id, **expected)
        operation_id, tokens = prepared.operation.id, prepared.leases
        terminal = _public(prepared.operation, "terminal") if prepared.operation.state in transitions.TERMINAL else None
        db.commit()
    if tokens:
        with session_factory() as db:
            for token in tokens:
                leases.release(db, token)
            db.commit()
    if terminal is not None:
        return terminal
    return resume_one(session_factory, operation_id, identity, **expected)
