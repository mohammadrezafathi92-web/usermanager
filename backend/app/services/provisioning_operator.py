"""Superadmin operator actions on durable operations and the installation
namespace (Lifecycle 11.5, 13, 10.10). Each is one explicit step; the route
checks the superadmin and the admin's own password; nothing here is automatic.

  view         GET operations/{id}: state and steps, allowlisted fields only
               (transitions.public_step) - no credential, no raw error.
  retry        a cleanup_required operation (not an approval whose binding
               was lost - that is approval_recovery's) is tried again on its
               SAFE side: a create operation is compensated (every step
               removed by its own identity - a cleanup state already erased
               the staged credential, so rolling forward is impossible by
               design); a deletion removes again. An identity that is still
               not ours stays unverified -> cleanup_required -> force-step.
  force_step   one cleanup_required step -> removed / remote_outcome
               'abandoned' with the admin, the reason and the time; then the
               operation is compensated (deletion: finished) by the worker.
               The abandoned step itself is the orphan's audit row.
  orphans      every abandoned step (node, backend, non-secret identity).
  rotate       a new installation_uuid in the column and the file (10.10);
               fork=True is the clone reset (all types legacy, gate off, no
               owner, every node contract invalidated).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import secrets
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from .. import models_provisioning as mp
from . import gate_locks, provisioning_installation, provisioning_schema, resource_leases
from . import provisioning_transitions as transitions
from .provisioning_host import HostIdentityUnavailable, read_identity

logger = logging.getLogger(__name__)
FORK_CONFIRMATION = "این یک نصب مستقل جدید است و نصب اصلی جدا ادامه می‌دهد"
MANUAL_XRAY_ABANDON_CONFIRMATION = "I understand this records an unverified manual Xray removal"
_OPERATION_FIELDS = ("id", "operation_type", "state", "tenant_scope_key", "actor_kind", "actor_id", "approval_uuid",
                     "approval_execution_version", "target_user_id", "result_user_id", "result_purchase_id",
                     "error_code", "attempts", "next_retry_at", "forward_deadline", "created_at", "completed_at",
                     "version")


def _reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(422, "reason_required")
    return reason[:500]


def view(db, operation_id: int) -> dict:
    provisioning_schema.assert_ready()
    operation = db.get(mp.ProvisioningOperation, operation_id)
    if operation is None:
        raise HTTPException(404, "provisioning_operation_not_found")
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                       .order_by(mp.ProvisioningStep.step_order, mp.ProvisioningStep.id)).scalars().all()
    out = {name: getattr(operation, name) for name in _OPERATION_FIELDS}
    out["steps"] = [transitions.public_step(step) for step in steps]
    return out


def orphans(db) -> list:
    rows = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.remote_outcome == "abandoned")
                      .order_by(mp.ProvisioningStep.id)).scalars().all()
    return [dict(step_id=row.id, operation_id=row.operation_id, node_id=row.node_id, backend=row.backend,
                 protocol=row.protocol, direction=row.direction, wg_peer_name=row.wg_peer_name,
                 wg_public_key=row.wg_public_key, account_username=row.account_username, xr_email=row.xr_email,
                 forced_by_admin_id=row.forced_by_admin_id, force_reason=row.force_reason, forced_at=row.forced_at)
            for row in rows]


def _locked_operation(db, operation_id):
    from . import provisioning_approval_exec as approval_exec
    db.execute(select(mp.ProvisioningRuntimeState.id).where(mp.ProvisioningRuntimeState.id == 1)
               .with_for_update(read=True)).all()
    approval_exec.lock_for(db, operation_id)          # approval row before the operation (lock order)
    operation = transitions._operation(db, operation_id)
    if operation.state != "cleanup_required":
        raise HTTPException(409, "provisioning_operation_not_cleanup_required")
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                       .order_by(mp.ProvisioningStep.step_order).with_for_update()
                       .execution_options(populate_existing=True)).scalars().all()
    return operation, steps


def retry(session_factory, admin_id: int, operation_id: int, reason: str) -> dict:
    from . import provisioning_approval_exec as approval_exec
    reason = _reason(reason)
    with session_factory() as db:
        resource_leases.begin_business(db)
        operation, steps = _locked_operation(db, operation_id)
        if operation.approval_uuid is not None and operation.error_code in approval_exec.LOST_CODES:
            raise HTTPException(409, "approval_recovery_required")
        if operation.operation_type.startswith("delete_"):
            for step in steps:
                if step.state == "cleanup_required":
                    transitions._write_step(db, step, "staged", remote_outcome=None, error_code=None,
                                            next_retry_at=None)
            transitions._write_operation(db, operation, "provisioning", error_code=None, next_retry_at=None)
        else:
            for step in steps:
                if step.state == "cleanup_required":
                    transitions._write_step(db, step, "compensating", remote_outcome=None, error_code=None,
                                            next_retry_at=None)
            # Every other step that is not removed yet is removed too; the
            # original permanent cause was overwritten by the cleanup code.
            transitions.begin_compensation(db, operation.id, "step_failed_permanent")
        db.commit()
    logger.info("provisioning operation %s retried by admin %s", operation_id, admin_id)
    return dict(operation_id=operation_id, retried=True)


def force_step(session_factory, admin_id: int, operation_id: int, step_id: int, reason: str) -> dict:
    reason = _reason(reason)
    with session_factory() as db:
        resource_leases.begin_business(db)
        operation, steps = _locked_operation(db, operation_id)
        step = next((row for row in steps if row.id == step_id), None)
        if step is None or step.state != "cleanup_required":
            raise HTTPException(409, "provisioning_step_not_cleanup_required")
        transitions._write_step(db, step, "removed", remote_outcome="abandoned", forced_by_admin_id=admin_id,
                                force_reason=reason, forced_at=dt.datetime.utcnow(), next_retry_at=None)
        if not any(row.state == "cleanup_required" for row in steps if row.id != step_id):
            if operation.operation_type.startswith("delete_"):
                transitions._write_operation(db, operation, "provisioning", error_code=None, next_retry_at=None)
            else:
                transitions.begin_compensation(db, operation.id, "step_failed_permanent")
        db.commit()
    logger.info("provisioning step %s of operation %s forced abandoned by admin %s", step_id, operation_id, admin_id)
    return dict(operation_id=operation_id, step_id=step_id, forced=True)


def abandon_unknown_xray_delete(session_factory, admin_id: int, operation_id: int, step_id: int,
                                reason: str, confirmation: str, acknowledged: bool) -> dict:
    """Record an admin-attested manual Xray deletion after an unknown write.

    This is deliberately narrower than force_step: only a delete_purchase
    step for Xray on 3X-UI, still marked remote_result_unknown, can be
    abandoned. It does NOT claim remote absence. The durable step remains as
    an audit row and the ordinary deletion worker must finish the remaining
    steps and finalization.
    """
    reason = _reason(reason)
    if any(type(value) is not int or value < 1 for value in (admin_id, operation_id, step_id)):
        raise HTTPException(422, "manual_xray_abandon_identity_invalid")
    if confirmation != MANUAL_XRAY_ABANDON_CONFIRMATION or acknowledged is not True:
        raise HTTPException(422, "manual_xray_abandon_confirmation_required")
    from . import provisioning_approval_exec as approval_exec, remote_runner

    owner = f"op:{operation_id}:{secrets.randbits(64) + 1}"
    lease = None
    with session_factory() as db:
        resource_leases.begin_business(db)
        db.execute(select(mp.ProvisioningRuntimeState.id).where(mp.ProvisioningRuntimeState.id == 1)
                   .with_for_update(read=True)).all()
        approval_exec.lock_for(db, operation_id)
        try:
            lease = resource_leases.take_over_deployment_hold(db, operation_id, owner, ttl=60)
            if lease is None:
                lease = resource_leases.acquire(db, f"provisioning_op:{operation_id}", owner, ttl=60)
        except resource_leases.LeaseBusy:
            raise HTTPException(409, "provisioning_operation_busy") from None
        resource_leases.revalidate(db, (lease,))
        operation = transitions._operation(db, operation_id)
        steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
                           .order_by(mp.ProvisioningStep.step_order).with_for_update()
                           .execution_options(populate_existing=True)).scalars().all()
        step = next((row for row in steps if row.id == step_id), None)
        if step is not None and step.state == "removed" and step.remote_outcome == "abandoned":
            resource_leases.release(db, lease)
            db.commit()
            return {"operation_id": operation_id, "step_id": step_id, "abandoned": True}
        if operation.operation_type != "delete_purchase" or operation.state != "provisioning":
            raise HTTPException(409, "manual_xray_abandon_operation_invalid")
        if step is None or (step.direction, step.backend, step.protocol, step.state, step.error_code,
                            step.remote_outcome, bool(step.remote_attempted)) != (
                "remove", "threexui", "xray", "remote_calling", "remote_result_unknown", None, True):
            raise HTTPException(409, "manual_xray_abandon_step_invalid")
        if not step.xr_email or step.xr_panel_inbound_id is None:
            raise HTTPException(409, "manual_xray_abandon_identity_missing")
        if any(row.id != step.id and row.state == "remote_calling" for row in steps):
            raise HTTPException(409, "provisioning_remote_step_in_flight")
        if remote_runner.live_count():
            raise HTTPException(409, "remote_runner_alive")

        now = dt.datetime.utcnow()
        transitions._write_step(db, step, "removed", remote_outcome="abandoned", forced_by_admin_id=admin_id,
                                force_reason=reason, forced_at=now, next_retry_at=None, error_code=None)
        transitions._write_operation(db, operation, "provisioning", error_code=None, next_retry_at=None)
        resource_leases.release(db, lease)
        db.commit()
    logger.warning("provisioning step %s of operation %s manually abandoned by admin %s",
                   step_id, operation_id, admin_id)
    return {"operation_id": operation_id, "step_id": step_id, "abandoned": True}


# ------------------------------------------------------------------ installation (10.10)
def _publish_file(installation_uuid: str, base_dir=None) -> None:
    """Atomic replace of {lock_dir}/installation.id (0600)."""
    directory = base_dir or gate_locks.lock_base_dir()
    temporary = os.path.join(directory, ".installation-" + secrets.token_hex(12) + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, (installation_uuid + "\n").encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, os.path.join(directory, "installation.id"))
    folder = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(folder)
    finally:
        os.close(folder)


def rotate(session_factory, admin_id: int, *, reason: str, fork: bool = False, confirmation: str = "",
           base_dir=None) -> dict:
    """A new namespace. Allowed from a mismatched file too (that is one of
    its two remedies). The database column is the reference: it is written
    first; a crash before the file is written leaves a mismatch, which is
    fail-closed and resolved by running this again."""
    from . import remote_runner
    from .provisioning_transitions import TERMINAL
    reason = _reason(reason)
    if fork and confirmation != FORK_CONFIRMATION:
        raise HTTPException(422, "fork_confirmation_mismatch")
    provisioning_schema.assert_ready()
    with session_factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        old = row.installation_uuid
    hold = gate_locks.FileLock(gate_locks.mode_lock_path(old, base_dir))
    try:
        hold.acquire(shared=False, timeout=5)
    except (gate_locks.LockTimeout, gate_locks.LockUnavailable):
        raise HTTPException(409, "installation_locks_held") from None
    try:
        with session_factory() as db:
            try:
                if db.get_bind().dialect.name == "sqlite":
                    from sqlalchemy import text
                    db.execute(text("BEGIN IMMEDIATE"))
                row = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
                                 .with_for_update().execution_options(populate_existing=True)).scalar_one()
                if db.execute(select(mp.ProvisioningOperation.id).where(
                        mp.ProvisioningOperation.state.notin_(TERMINAL)).limit(1)).first() is not None:
                    raise HTTPException(409, "provisioning_operations_pending")
                now = dt.datetime.utcnow()
                new = str(uuid.uuid4())
                if fork:
                    try:
                        identity = read_identity()
                    except HostIdentityUnavailable:
                        raise HTTPException(409, "host_identity_unavailable") from None
                    if row.owner_host_id is not None and row.owner_host_id == identity.host_id:
                        raise HTTPException(409, "fork_on_original_owner_host")
                    db.execute(mp.ProvisioningTypeMode.__table__.update().values(
                        mode="legacy", changed_at=now, version=mp.ProvisioningTypeMode.version + 1))
                    db.execute(mp.ProvisioningNodeContract.__table__.update().where(
                        mp.ProvisioningNodeContract.state == "ready").values(
                        state="invalidated", invalidated_at=now, invalidation_reason="manual",
                        version=mp.ProvisioningNodeContract.version + 1))
                    db.execute(mp.ProvisioningRuntimeState.__table__.update().where(
                        mp.ProvisioningRuntimeState.id == 1).values(
                        installation_uuid=new, gate_mode="off", gate_mode_epoch=row.gate_mode_epoch + 1,
                        gate_mode_changed_at=now, owner_state="none", owner_host_id=None, owner_boot_id=None,
                        owner_claimed_at=None, owner_heartbeat_at=None, changed_at=now,
                        version=mp.ProvisioningRuntimeState.version + 1))
                    event = "installation_fork_reset"
                else:
                    if row.gate_mode != "off" or row.owner_state not in ("none", "released") or db.execute(
                            select(mp.ProvisioningTypeMode.operation_type).where(
                                mp.ProvisioningTypeMode.mode != "legacy")).first() is not None:
                        raise HTTPException(409, "installation_rotate_not_inert")
                    if remote_runner.live_count():
                        raise HTTPException(409, "installation_runner_alive")
                    db.execute(mp.ProvisioningRuntimeState.__table__.update().where(
                        mp.ProvisioningRuntimeState.id == 1).values(
                        installation_uuid=new, changed_at=now, version=mp.ProvisioningRuntimeState.version + 1))
                    event = "installation_rotate"
                db.add(mp.ProvisioningOwnershipEvent(event_type=event, from_host_id=row.owner_host_id,
                                                     to_host_id=None if fork else row.owner_host_id,
                                                     ownership_epoch=row.ownership_epoch, actor_admin_id=admin_id,
                                                     reason=reason))
                db.commit()
            except Exception:
                db.rollback()
                raise
        try:
            _publish_file(new, base_dir)
        except OSError:
            raise HTTPException(409, "installation_file_write_failed") from None
    finally:
        hold.release()
    from . import node_gate
    node_gate.reset_cache()
    provisioning_installation.read_file(base_dir)
    return dict(rotated=True, fork=fork, event=event)
