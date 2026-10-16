"""Production entry of the durable path (Lifecycle 13, 11.1).

A caller whose operation type is 'durable' builds a trusted Preparation (or
DeleteRequest) and calls run(): T1 is committed, then the request thread
advances the operation step by step until it is terminal or the request
budget runs out (Lifecycle 18 decision 3: 60 s):

  completed     -> the committed operation (the caller rebuilds today's
                   response body from the result rows)
  compensated   -> HTTP 400 with the operation's error code (nothing left
                   behind, money released)
  otherwise     -> HTTP 409 {"error": "operation_in_progress", ...}; the
                   worker below finishes it, and the same idempotency key
                   asks again

recover_due() is the background worker: every RECOVER_SECONDS it advances
due operations (expired lease, retry time reached, forward deadline passed)
of every durable type, never one with a live lease. It runs only on the
host that owns the installation, and never in HA without a coordinator.

The owner identity used for fencing is always read fresh: this host's id
and boot, the installation and the ownership epoch of the singleton row.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from . import provisioning_schema, resource_leases
from .provisioning_host import HostIdentityUnavailable, read_identity
from .provisioning_transitions import TERMINAL

logger = logging.getLogger(__name__)
REQUEST_BUDGET_SECONDS = 60          # Lifecycle 18 decision 3 (default)
RECOVER_SECONDS = 15
_PAUSE = {"waiting": 0.5, "lease_busy": 0.25, "draining": 0.5}


def type_mode(db, operation_type: str) -> str:
    """'durable' or 'legacy' for one operation type (plain read; T1 re-checks
    it under the locking read of its own transaction)."""
    if not provisioning_schema.is_ready():
        return "legacy"
    mode = db.execute(select(mp.ProvisioningTypeMode.mode).where(
        mp.ProvisioningTypeMode.operation_type == operation_type)).scalar_one_or_none()
    return "durable" if mode == "durable" else "legacy"


def is_durable(db, operation_type: str) -> bool:
    return type_mode(db, operation_type) == "durable"


def username_claimed(db, username: str) -> bool:
    """A non-terminal durable create_user holds this username (5.1
    username_claim): a legacy creation must not take it meanwhile."""
    if not provisioning_schema.is_ready():
        return False
    return db.execute(select(mp.ProvisioningOperation.id).where(
        mp.ProvisioningOperation.username_claim == username)).first() is not None


def idempotency_key(request_header) -> str:
    """The caller's X-Idempotency-Key, or a fresh one (an old caller's retry
    is then a new request, exactly as today)."""
    # In-process callers (telegram_bot/panel_bridge) call the route functions
    # directly; an omitted header parameter then arrives as FastAPI's own
    # default object, which is "no key", not a key.
    value = request_header.strip() if isinstance(request_header, str) else ""
    if value:
        try:
            return str(uuid.UUID(value))
        except ValueError:
            raise HTTPException(422, "idempotency_key_invalid") from None
    return str(uuid.uuid4())


def _owner(session_factory):
    try:
        identity = read_identity()
    except HostIdentityUnavailable:
        raise HTTPException(503, "host_identity_unavailable") from None
    with session_factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1, populate_existing=True)
        if row is None or row.owner_state not in ("active", "draining") or (
                row.owner_host_id, row.owner_boot_id) != (identity.host_id, identity.boot_id):
            raise HTTPException(503, "provisioning_owner_unavailable")
        panel = db.get(models.PanelSettings, 1)
        if panel is not None and panel.ha_enabled:
            raise HTTPException(503, "provisioning_ha_unsupported")
        return identity, dict(installation_uuid=row.installation_uuid, ownership_epoch=int(row.ownership_epoch))


def _in_progress(session_factory, operation_id):
    """A lease that expired by the DB clock while this request held it (the
    process stalled past its TTL - observed: the host slept mid-request).
    The fencing already refused the stale transaction; the committed
    remote_calling marker makes the next owner recover by reading the remote
    first. The caller is told the truth: still in progress, retry later."""
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        state = operation.state if operation is not None else None
    logger.warning("provisioning operation %s: lease lost mid-request; left to recovery", operation_id)
    return HTTPException(409, {"error": "operation_in_progress", "operation_id": operation_id, "state": state})


def _finish(session_factory, operation_id, deadline, step):
    result = None
    while True:
        with session_factory() as db:
            operation = db.get(mp.ProvisioningOperation, operation_id)
            if operation is None:
                raise HTTPException(409, "provisioning_operation_changed")
            state, error = operation.state, operation.error_code
        if state in TERMINAL:
            break
        if state == "cleanup_required" or time.monotonic() >= deadline:
            raise HTTPException(409, {"error": "operation_in_progress", "operation_id": operation_id, "state": state})
        try:
            result = step(operation_id)
        except resource_leases.LeaseLost:
            raise _in_progress(session_factory, operation_id) from None
        pause = _PAUSE.get((result or {}).get("status"))
        if pause:
            time.sleep(pause)
    if state == "compensated":
        raise HTTPException(400, {"error": error or "provisioning_failed", "operation_id": operation_id})
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        db.expunge(operation)
        return operation


def _run_renewal(session_factory, request, identity, owner, *, actor_kind, actor_id, approval=None):
    """renew_user / renew_purchase are ONE transaction (Lifecycle 7.4): T1
    and T_final together. Afterwards, best effort, the existing
    reconciliation re-enables connections of a service that was cut off (a
    failure there never undoes the renewal; the poller applies it later)."""
    from . import provisioning_preparation as preparation, resource_leases as leases, user_ops
    from . import provisioning_approval_exec as approval_exec, provisioning_approval_final as approval_final
    targets = []
    try:
        with session_factory() as db:
            leases.begin_business(db)
            prepared = preparation.prepare(db, request, identity=identity, actor_kind=actor_kind, actor_id=actor_id,
                                           approval=approval, **owner)
            operation = prepared.operation
            for target in getattr(operation, "_reconciliation_after_commit", None) or []:
                targets.append((type(target).__name__, target.id))
            operation_id, tokens = operation.id, prepared.leases
            db.commit()
    except (approval_exec.ApprovalCancel, approval_final.ApprovalEffectFailed) as exc:
        # One transaction: nothing of it committed. The approval is cancelled
        # on its own (a 'required' renewal whose effects cannot be valid too).
        approval_exec.cancel(session_factory, approval.approval_uuid, approval.version, exc.code)
        raise HTTPException(409, exc.code) from None
    if tokens:
        with session_factory() as db:
            for token in tokens:
                leases.release(db, token)
            db.commit()
    for kind, ident in targets:
        try:
            with session_factory() as db:
                if kind == "Purchase":
                    target = db.get(models.Purchase, ident)
                    if target is not None:
                        user_ops.reconcile_purchase_connections(db, target)
                else:
                    target = db.get(models.User, ident)
                    if target is not None:
                        user_ops.reconcile_user_connections(db, target)
                db.commit()
        except Exception:  # noqa: BLE001 - the renewal is committed; the poller converges
            logger.exception("renewal reconciliation failed for %s %s", kind, ident)
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation.state != "completed":
            raise HTTPException(409, {"error": "operation_in_progress", "operation_id": operation_id,
                                      "state": operation.state})
        db.expunge(operation)
        return operation


def run(session_factory, request, *, actor_kind="system", actor_id=None, budget=None, approval=None):
    """Run one durable operation from the request thread (see module doc).
    `approval`: provisioning_approval_exec.ApprovalExecution of a receipt
    approval's production call (T1 binds it; 7.6)."""
    budget = REQUEST_BUDGET_SECONDS if budget is None else budget
    if getattr(request, "operation_type", "").startswith("renew_"):
        identity, owner = _owner(session_factory)
        return _run_renewal(session_factory, request, identity, owner, actor_kind=actor_kind, actor_id=actor_id,
                            approval=approval)
    from . import provisioning_delete_preparation as delete_preparation
    from . import provisioning_deletion_worker as deletion_worker
    from . import provisioning_operation_worker as operation_worker
    identity, owner = _owner(session_factory)
    deadline = time.monotonic() + budget
    worker = deletion_worker if isinstance(request, delete_preparation.DeleteRequest) else operation_worker
    extra = {"approval": approval} if approval is not None else {}
    try:
        first = worker.start_once(session_factory, request, identity, actor_kind=actor_kind, actor_id=actor_id,
                                  **extra, **owner)
    except resource_leases.LeaseLost:
        # T1 committed (only a committed operation has leases to lose).
        from . import provisioning_requests
        with session_factory() as db:
            operation = provisioning_requests.existing_operation(db, request.operation_type, request.business_key)
            operation_id = operation.id if operation is not None else None
        if operation_id is None:
            raise
        raise _in_progress(session_factory, operation_id) from None
    operation_id = first["operation_id"]
    return _finish(session_factory, operation_id, deadline,
                   lambda op_id: worker.resume_one(session_factory, op_id, identity, **owner))


def resume(session_factory, operation_id: int, *, budget=None):
    """Advance an EXISTING create/delete operation from the request thread
    (no T1: a retry that found its operation by business key). Same result
    contract as run()."""
    from . import provisioning_deletion_worker as deletion_worker
    from . import provisioning_operation_worker as operation_worker
    budget = REQUEST_BUDGET_SECONDS if budget is None else budget
    identity, owner = _owner(session_factory)
    with session_factory() as db:
        operation = db.get(mp.ProvisioningOperation, operation_id)
        if operation is None:
            raise HTTPException(404, "provisioning_operation_not_found")
        deleting = operation.operation_type.startswith("delete_")
    worker = deletion_worker if deleting else operation_worker
    return _finish(session_factory, operation_id, time.monotonic() + budget,
                   lambda op_id: worker.resume_one(session_factory, op_id, identity, **owner))


def recover_due(session_factory=None, *, max_operations: int = 50) -> int:
    """Background worker pass. Returns how many operations it advanced."""
    if session_factory is None:
        from ..database import SessionLocal as session_factory  # noqa: N813
    if not provisioning_schema.is_ready():
        return 0
    try:
        with session_factory() as db:
            if db.execute(select(mp.ProvisioningTypeMode.operation_type).where(
                    mp.ProvisioningTypeMode.mode == "durable")).first() is None:
                return 0
        identity, owner = _owner(session_factory)
    except HTTPException:
        return 0
    from . import provisioning_due_deletions as due_deletions, provisioning_due_operations as due_operations
    advanced = 0
    for module in (due_operations, due_deletions):
        cursor = 0
        while advanced < max_operations:
            try:
                outcome = module.recover_due_once(session_factory, identity, after_id=cursor, **owner)
            except HTTPException as exc:
                logger.warning("durable recovery pass stopped: %s", exc.detail)
                break
            except Exception:  # noqa: BLE001 - one operation never stops the worker
                logger.exception("durable recovery pass failed")
                break
            if not outcome or outcome.get("status") == "idle":
                break
            cursor = outcome["selection_cursor"]
            advanced += 1
    return advanced
