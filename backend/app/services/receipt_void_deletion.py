"""Steps 3 - 5 of a receipt void (deprovision:{connection_id},
verify:{connection_id}, db_delete) on the EXISTING durable deletion machine
(Lifecycle 12; Receipt Void 13.2 / 13.7): one delete_user (new_user, renew)
or delete_purchase (existing_user) provisioning operation per void, with a
business key derived from the void operation - so a retry, a crash, a
second worker always find the same one. That machine removes each
connection by its own identity (the only remote call), records the
absence outcome per connection, and runs the DB-only finalizer in its
T_final only when every one is absent or abandoned. This module only starts
or advances it and reads back what it recorded; it never calls a node, and
never deletes a row itself.

  verified_absent / delete_idempotently_absent  -> deprovision done, verify done
  radius_ppp (DB-only, the panel is the server)  -> done, no remote outcome
  abandoned (provisioning force-step)           -> both abandoned
  cleanup_required                              -> verify blocked 'unverified'
  anything else                                 -> still running
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from . import provisioning_requests

ABSENT = ("verified_absent", "delete_idempotently_absent")


def key(operation_id: int) -> str:
    return f"receipt_void:{operation_id}"


def business_key(delete: dict, operation_id: int) -> str:
    return f"delete:{delete['kind']}:{delete['id']}:{key(operation_id)}"


def provisioning_operation(db, delete: dict, operation_id: int):
    return provisioning_requests.existing_operation(db, f"delete_{delete['kind']}", business_key(delete, operation_id))


def durable_ready(db, delete: dict) -> bool:
    """The deletion type this scope needs is on the durable machine (else
    the void would wait for a machine that never runs it)."""
    mode = db.execute(select(mp.ProvisioningTypeMode.mode).where(
        mp.ProvisioningTypeMode.operation_type == f"delete_{delete['kind']}")).scalar()
    return mode == "durable"


def start_or_advance(session_factory, ctx: dict, operation_id: int, *, budget: float) -> None:
    """One bounded push of the provisioning operation. Its own failures stay
    on it (retry/backoff, cleanup_required); an error here (not durable, no
    owner, a lease held elsewhere) is the caller's step failure."""
    from . import provisioning_durable
    delete = ctx["delete"]
    with session_factory() as db:
        existing = provisioning_operation(db, delete, operation_id)
        if existing is not None:
            operation_id_p6, state = existing.id, existing.state
            request = None
        else:
            user = db.get(models.User, ctx["user_id"])
            if user is None:
                raise HTTPException(409, "receipt_void_target_missing")
            request = provisioning_requests.delete(db, delete["kind"], delete["id"], user.owner_admin_id,
                                                   key=key(operation_id))
    try:
        if request is not None:
            provisioning_durable.run(session_factory, request, actor_kind="admin", actor_id=ctx["actor_admin_id"],
                                     budget=budget)
        elif state not in ("completed", "cleanup_required", "compensated"):
            provisioning_durable.resume(session_factory, operation_id_p6, budget=budget)
    except HTTPException as exc:
        detail = exc.detail if isinstance(exc.detail, dict) else {"error": exc.detail}
        if detail.get("error") != "operation_in_progress":
            raise


def outcome(db, ctx: dict, operation_id: int) -> dict:
    """What the provisioning operation recorded, per connection of the void."""
    operation = provisioning_operation(db, ctx["delete"], operation_id)
    if operation is None:
        return {"state": "not_started", "connections": {}}
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation.id)
                       .order_by(mp.ProvisioningStep.step_order)).scalars().all()
    connections = {}
    for step in steps:
        if step.state == "removed" and step.remote_outcome == "abandoned":
            result = "abandoned"
        elif step.state == "removed" and (step.backend == "radius_ppp" or step.remote_outcome in ABSENT):
            result = "absent"
        elif step.state == "cleanup_required":
            result = "unverified"
        else:
            result = "running"
        connections[step.connection_id] = {
            "result": result, "remote_outcome": step.remote_outcome, "backend": step.backend,
            "node_id": step.node_id, "protocol": step.protocol, "provisioning_step_id": step.id,
            "remote_attempted": bool(step.remote_attempted), "error": step.error_code,
            "forced_by_admin_id": step.forced_by_admin_id}
    state = {"completed": "completed", "cleanup_required": "cleanup_required"}.get(operation.state, "running")
    if operation.state in ("compensated", "failed"):
        state = "cleanup_required"
    return {"state": state, "provisioning_operation_id": operation.id, "provisioning_state": operation.state,
            "error": operation.error_code, "connections": connections}
