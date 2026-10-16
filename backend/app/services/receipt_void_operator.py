"""Operator side of the receipt void (Receipt Void 13.3, 13.5, 16, 18):
status, retry, force-step, audit, and the background worker pass. Every
entry point is checked against the approval's owner snapshot
(receipt_void_scope.find: missing and not allowed are both 404); the routes
add superadmin, and the admin's password for force.

Retry and force are gated by the WORKER flag, not the execute flag:
switching execute off stops new voids, never the cleanup of running ones.
"""
from __future__ import annotations

import datetime as dt
import json
import logging

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_receipt_void as rv
from . import receipt_void_deletion as deletion, receipt_void_engine as engine, receipt_void_runner as runner
from . import receipt_void_flags as flags, receipt_void_scope as scope, receipt_void_schema

logger = logging.getLogger(__name__)
_OPERATION_FIELDS = ("id", "state", "actor_id", "actor_ip", "reason", "attempts", "created_at", "started_at",
                     "completed_at", "version")
_STEP_FIELDS = ("step_key", "step_order", "state", "remote_outcome", "attempts", "next_retry_at", "error_sanitized",
                "completed_at")


def _authorized(db, admin, operation_id: int, *, lock=False) -> tuple[dict, dict]:
    operation = engine._operation(db, operation_id, lock=lock)
    approval = scope.find(db, admin, approval_uuid=operation["business_key"])
    return operation, approval


def _public(db, operation: dict, approval: dict) -> dict:
    out = {name: operation[name] for name in _OPERATION_FIELDS}
    out["operation_id"] = out.pop("id")
    ctx = engine.context(operation)
    out["approval"] = {"approval_uuid": approval["approval_uuid"], "state": approval["state"],
                       "kind": approval["kind"], "target_shape": approval["target_shape"],
                       "username": approval["target_username_snapshot"]}
    out["steps"] = []
    for step in engine._steps(db, operation["id"]):
        row = {name: step[name] for name in _STEP_FIELDS}
        row["result"] = json.loads(step["result_snapshot"] or "{}")
        out["steps"].append(row)
    out["forced"] = ctx.get("force_log") or []
    return out


def status(db, admin, operation_id: int) -> dict:
    receipt_void_schema.assert_ready()
    operation, approval = _authorized(db, admin, operation_id)
    return _public(db, operation, approval)


# ------------------------------------------------------------------ retry (13.3)
def retry(session_factory, admin, operation_id: int, *, reason=None, budget: float = 15.0) -> dict:
    from . import provisioning_operator
    with session_factory() as db:
        flags.require(db, "worker")
        operation, approval = _authorized(db, admin, operation_id)
        if operation["state"] not in ("cleanup_required", "failed"):
            raise HTTPException(409, "receipt_void_not_retryable")
        ctx = engine.context(operation)
        provisioning = deletion.provisioning_operation(db, ctx["delete"], operation_id) if ctx["delete"] else None
        provisioning_id = provisioning.id if provisioning is not None and provisioning.state == "cleanup_required" \
            else None
    if provisioning_id is not None:
        provisioning_operator.retry(session_factory, admin.id, provisioning_id, (reason or "receipt void retry"))
    with session_factory() as db:
        engine._lock_approval(db, operation["business_key"])
        operation = engine._operation(db, operation_id, lock=True)
        if operation["state"] == "failed":
            # Nothing was applied (block is one transaction), but the account
            # may have changed since: only the exact confirmed scope is retried;
            # otherwise 409 and a fresh preview + execute re-plans it.
            scope.eligible(scope.find(db, admin, approval_uuid=operation["business_key"]))
            engine.revalidate_scope(db, operation)
            engine._approval_state(db, operation["business_key"], ("completed",), "voiding",
                                   void_operation_id=operation_id)
        elif operation["state"] == "cleanup_required":
            engine._approval_state(db, operation["business_key"], ("cleanup_required",), "voiding")
        else:
            raise HTTPException(409, "receipt_void_not_retryable")
        for step in engine._steps(db, operation_id):
            if step["state"] in ("failed", "blocked"):
                engine._write_step(db, step["id"], state="pending", remote_outcome=None, attempts=0,
                                   next_retry_at=None)
        engine._bump(db, operation, state="running", completed_at=None)
        db.commit()
    logger.info("receipt void %s retried by admin %s", operation_id, admin.id)
    runner.advance(session_factory, operation_id, budget=budget)
    with session_factory() as db:
        return engine._public_state(db, operation_id)


# ------------------------------------------------------------------ force (13.5, 14.1)
def force_step(session_factory, admin, operation_id: int, *, step_key: str, reason, danger_ack,
               budget: float = 15.0) -> dict:
    from . import provisioning_operator
    from .. import models_provisioning as mp
    if danger_ack is not True:
        raise HTTPException(422, "danger_ack_required")
    reason = engine._reason(reason)
    with session_factory() as db:
        flags.require(db, "worker")
        operation, _approval = _authorized(db, admin, operation_id)
        if operation["state"] != "cleanup_required":
            raise HTTPException(409, "receipt_void_step_not_forceable")
        ctx = engine.context(operation)
        step = next((s for s in engine._steps(db, operation_id) if s["step_key"] == step_key), None)
        if step is None or step["state"] not in ("failed", "blocked"):
            raise HTTPException(409, "receipt_void_step_not_forceable")
        provisioning_step = None
        if step_key.startswith(engine.DELETION_PREFIXES):
            cid = int(step_key.split(":", 1)[1])
            provisioning = deletion.provisioning_operation(db, ctx["delete"], operation_id)
            row = None if provisioning is None else db.execute(select(mp.ProvisioningStep).where(
                mp.ProvisioningStep.operation_id == provisioning.id,
                mp.ProvisioningStep.connection_id == cid)).scalar_one_or_none()
            if row is None or row.state != "cleanup_required":
                raise HTTPException(409, "receipt_void_step_not_forceable")
            provisioning_step = (provisioning.id, row.id)
        elif step_key not in engine.FORCEABLE_LOCAL or step["error_sanitized"] != "quota_changed_since_reward":
            raise HTTPException(409, "receipt_void_step_not_forceable")
    if provisioning_step is not None:
        provisioning_operator.force_step(session_factory, admin.id, provisioning_step[0], provisioning_step[1], reason)
    with session_factory() as db:
        engine._lock_approval(db, operation["business_key"])
        operation = engine._operation(db, operation_id, lock=True)
        if operation["state"] != "cleanup_required":
            raise HTTPException(409, "receipt_void_step_not_forceable")
        ctx = engine.context(operation)
        entry = {"step_key": step_key, "admin_id": admin.id, "reason": reason,
                 "at": dt.datetime.utcnow().isoformat(timespec="seconds")}
        ctx["force_log"] = (ctx.get("force_log") or []) + [entry]
        if provisioning_step is None:
            ctx["forced"] = sorted(set(ctx.get("forced") or ()) | {"quota:*"})
        rows = engine._steps(db, operation_id)
        for row in rows:
            if row["step_key"] == step_key or (provisioning_step and row["step_key"] in (
                    f"deprovision:{step_key.split(':', 1)[1]}", f"verify:{step_key.split(':', 1)[1]}")):
                if provisioning_step is None:
                    engine._write_step(db, row["id"], state="pending", attempts=0, next_retry_at=None,
                                       result_snapshot={"forced": entry})
                else:
                    engine._write_step(db, row["id"], state="abandoned", remote_outcome="abandoned",
                                       next_retry_at=None, result_snapshot={"forced": entry,
                                                                            "orphan_for_manual_cleanup": True})
        rows = engine._steps(db, operation_id)
        if not any(r["state"] == "blocked" or (r["state"] == "failed" and r["step_key"] != "db_delete")
                   for r in rows):
            for row in rows:
                if row["state"] == "failed":
                    engine._write_step(db, row["id"], state="pending", attempts=0, next_retry_at=None)
            engine._approval_state(db, operation["business_key"], ("cleanup_required",), "voiding")
            state = "running"
        else:
            state = "cleanup_required"
        snapshot = json.loads(operation["result_snapshot"])
        snapshot["context"] = ctx
        engine._bump(db, operation, state=state, result_snapshot=json.dumps(snapshot, sort_keys=True))
        db.commit()
    logger.info("receipt void %s step %s forced by admin %s", operation_id, step_key, admin.id)
    if state == "running":
        runner.advance(session_factory, operation_id, budget=budget)
    with session_factory() as db:
        return engine._public_state(db, operation_id)


# ------------------------------------------------------------------ audit (16)
def audit(db, admin, approval_uuid: str) -> dict:
    receipt_void_schema.assert_ready()
    approval = scope.find(db, admin, approval_uuid=approval_uuid)
    operation = engine._by_approval(db, approval_uuid)
    effects_table = rv.receipt_approval_effects
    own = [dict(r) for r in db.execute(select(effects_table).where(
        effects_table.c.approval_uuid == approval_uuid).order_by(effects_table.c.id)).mappings()]
    entries = db.query(models.LedgerEntry).filter(models.LedgerEntry.approval_uuid == approval_uuid).order_by(
        models.LedgerEntry.id).all()
    out = {
        "approval": {k: approval[k] for k in ("approval_uuid", "kind", "target_shape", "state", "amount_snapshot",
                                              "approval_mode", "target_username_snapshot", "telegram_id_snapshot",
                                              "completed_at", "voided_at", "owner_admin_id_snapshot")},
        "operation": _public(db, operation, approval) if operation else None,
        "effects": [_effect(r) for r in own],
        "ledger": [{"id": e.id, "kind": e.kind, "amount": int(e.amount or 0), "voided_at": e.voided_at,
                    "reversal_of_id": e.reversal_of_id, "created_at": e.created_at} for e in entries],
    }
    if operation is not None:
        op = operation["id"]
        out["other_approvals_effects_reversed"] = [_effect(dict(r)) | {"approval_uuid": r["approval_uuid"]}
                                                   for r in db.execute(select(effects_table).where(
                                                       effects_table.c.void_operation_id == op,
                                                       effects_table.c.approval_uuid != approval_uuid)).mappings()]
        out["debts"] = [{"id": int(r.id), "amount": int(r.amount), "state": r.state} for r in db.execute(
            select(rv.wallet_debts).where(rv.wallet_debts.c.source_void_operation_id == op))]
        out["writeoffs"] = [{"id": int(r.id), "amount": int(r.amount), "reason": r.reason} for r in db.execute(
            select(rv.wallet_writeoffs).where(rv.wallet_writeoffs.c.wallet_operation_id == op))]
        out["quota_shortfalls"] = [{"granted": int(r.granted_bytes), "reversed": int(r.reversed_bytes),
                                    "shortfall": int(r.shortfall_bytes)} for r in db.execute(
            select(rv.quota_reversal_shortfalls).where(rv.quota_reversal_shortfalls.c.wallet_operation_id == op))]
        out["loyalty_rewards_voided"] = [{"id": int(r.id), "reason": r.void_reason,
                                          "credit": r.credit_reversal_state, "quota": r.quota_reversal_state}
                                         for r in db.execute(select(rv.loyalty_reward_events).where(
                                             rv.loyalty_reward_events.c.void_operation_id == op))]
    return out


def _effect(row: dict) -> dict:
    return {k: row[k] for k in ("effect_type", "effect_key", "resource_type", "resource_id", "delta_value",
                                "reversal_state", "reversed_value", "voided_at", "void_operation_id")}


# ------------------------------------------------------------------ worker
def recover_due(session_factory=None, *, max_operations: int = 20) -> int:
    """Scheduler pass: every running void, while the worker flag is on."""
    if session_factory is None:
        from ..database import SessionLocal as session_factory  # noqa: N813
    if not receipt_void_schema.is_ready():
        return 0
    with session_factory() as db:
        if not flags.enabled(db, "worker"):
            return 0
        table = rv.wallet_operations
        ids = [int(r.id) for r in db.execute(select(table.c.id).where(
            table.c.operation_type == engine.OPERATION_TYPE, table.c.state == "running").order_by(table.c.id)
            .limit(max_operations))]
    for operation_id in ids:
        try:
            runner.advance(session_factory, operation_id, budget=10.0)
        except Exception:  # noqa: BLE001 - one void never stops the pass
            logger.exception("receipt void %s could not be advanced", operation_id)
    return len(ids)
