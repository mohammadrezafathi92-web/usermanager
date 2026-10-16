"""The durable receipt void (Receipt Void 13; P11):
wallet_operations(operation_type='void_receipt', business_key=approval_uuid)
and its wallet_operation_steps in the fixed order of 13.2.

  execute()     claim: validates the request (typed username, reason,
                danger_ack, the signed preview and its state hash), then ONE
                transaction: the operation, the approval completed -> voiding
                (CAS on its version), every step. The same request again,
                by any actor, returns the same operation; another payload
                for the approval is 409.
  advance()     from the first step that is not done/abandoned, one step at a
                time, each in its own transaction that first takes the phase
                read, then the operation row with a version CAS (two workers
                never both run a step) - see _local(). Steps 3 - 5 are the
                provisioning deletion machine (receipt_void_deletion).
  retry()       cleanup_required / failed -> running again, from the first
                step not done; the provisioning operation is retried too.
  force_step()  a deprovision:/verify: step of a cleanup_required void is
                abandoned through the provisioning force-step (an orphan for
                manual cleanup, with the admin and the reason); a step blocked
                by a quota changed since the reward is forced to not_applicable.
  status() / audit()   allowlisted, never a credential.

Failure (13.3): block fails -> operation failed, approval back to completed.
Any later step: retried with backoff, after MAX_ATTEMPTS (or a blocked
outcome at once) operation and approval -> cleanup_required.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import models_receipt_void as rv
from . import receipt_void_deletion as deletion, receipt_void_effects as void_effects
from . import receipt_void_flags as flags, receipt_void_scope as scope, receipt_void_steps as steps
from . import wallet_lots

logger = logging.getLogger(__name__)
OPERATION_TYPE = "void_receipt"
MAX_ATTEMPTS = 5
BACKOFF_SECONDS = (5, 30, 120, 600)
LOCAL = ("block", "loyalty_reconciliation", "wallet_reversal", "ledger_reversal", "card_reversal", "finalize")
DELETION_PREFIXES = ("deprovision:", "verify:")
FORCEABLE_LOCAL = ("loyalty_reconciliation", "ledger_reversal")


def _now():
    return dt.datetime.utcnow()


def _reason(reason) -> str:
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason:
        raise HTTPException(422, "reason_required")
    return reason[:500]


def _code(exc) -> str:
    if isinstance(exc, (steps.StepFailed, void_effects.StepBlocked)):
        return exc.code
    if isinstance(exc, HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("error") or detail.get("code")
        return str(detail)[:80] if isinstance(detail, str) and detail.isascii() else f"http_{exc.status_code}"
    return type(exc).__name__[:80]


# ------------------------------------------------------------------ rows
def _operation(db, operation_id, lock=False) -> dict:
    table = rv.wallet_operations
    query = select(table).where(table.c.id == operation_id, table.c.operation_type == OPERATION_TYPE)
    row = db.execute(wallet_lots._for_update(db, query) if lock else query).mappings().first()
    if row is None:
        raise HTTPException(404, "receipt_void_operation_not_found")
    return dict(row)


def _by_approval(db, approval_uuid) -> Optional[dict]:
    table = rv.wallet_operations
    row = db.execute(select(table).where(table.c.operation_type == OPERATION_TYPE,
                                         table.c.business_key == approval_uuid)).mappings().first()
    return dict(row) if row else None


def _steps(db, operation_id) -> list[dict]:
    table = rv.wallet_operation_steps
    return [dict(r) for r in db.execute(select(table).where(table.c.wallet_operation_id == operation_id)
                                        .order_by(table.c.step_order)).mappings()]


def context(operation: dict) -> dict:
    return json.loads(operation["result_snapshot"])["context"]


def _bump(db, operation: dict, **values) -> None:
    """The operation row CAS: the first write of every step transaction."""
    table = rv.wallet_operations
    changed = db.execute(table.update().where(table.c.id == operation["id"], table.c.version == operation["version"])
                         .values(version=table.c.version + 1, **values)).rowcount
    if changed != 1:
        raise HTTPException(409, "receipt_void_operation_changed")


def _write_step(db, step_id, **values) -> None:
    table = rv.wallet_operation_steps
    if "result_snapshot" in values and not isinstance(values["result_snapshot"], str):
        values["result_snapshot"] = json.dumps(values["result_snapshot"], sort_keys=True, default=str)
    db.execute(table.update().where(table.c.id == step_id).values(**values))


def _approval_state(db, approval_uuid, expected: tuple, state: str, **values) -> None:
    table = rv.receipt_approvals
    changed = db.execute(table.update().where(table.c.approval_uuid == approval_uuid, table.c.state.in_(expected))
                         .values(state=state, version=table.c.version + 1, **values)).rowcount
    if changed != 1:
        raise HTTPException(409, "approval_state_changed")


def step_keys(shape: str, connection_ids: list, deletes: bool) -> list[str]:
    keys = ["claim", "block"]
    if shape != "topup" and deletes:
        keys += [f"deprovision:{cid}" for cid in connection_ids] + [f"verify:{cid}" for cid in connection_ids]
        keys.append("db_delete")
    if shape != "topup":
        keys.append("loyalty_reconciliation")
    return keys + ["wallet_reversal", "ledger_reversal", "card_reversal", "finalize"]


# ------------------------------------------------------------------ execute (claim)
def _request_hash(approval_uuid, typed_username, reason) -> str:
    return wallet_lots.request_hash({"approval_uuid": approval_uuid, "typed_username": typed_username,
                                     "reason": reason})


def _public_state(db, operation_id) -> dict:
    operation = _operation(db, operation_id)
    return {"operation_id": operation["id"], "state": operation["state"]}


def execute(session_factory, admin, *, preview_token, typed_username, reason, danger_ack, ip=None,
            budget: float = 15.0) -> dict:
    if danger_ack is not True:
        raise HTTPException(422, "danger_ack_required")
    reason = _reason(reason)
    if not isinstance(typed_username, str) or not typed_username:
        raise HTTPException(422, "typed_username_required")
    with session_factory() as db:
        flags.require(db, "execute")
        token = scope.read_token(preview_token)
        approval = scope.find(db, admin, approval_uuid=token["u"])
        if typed_username != approval["target_username_snapshot"]:
            raise HTTPException(422, "typed_username_mismatch")
        digest = _request_hash(approval["approval_uuid"], typed_username, reason)
        # The claim is its own transaction whose FIRST reads are the locking
        # ones (the phase, then the approval row): no snapshot taken before
        # them can be stale (MariaDB 1020), and a second execute waits on
        # the approval row and then finds the first one's operation.
        db.rollback()
        operation_id, replayed = _claim(db, admin, approval["approval_uuid"], token["h"], digest, reason, ip)
    if replayed:
        with session_factory() as db:
            return dict(_public_state(db, operation_id), replayed=True)
    with session_factory() as db:
        worker = flags.enabled(db, "worker")
    if worker:
        advance(session_factory, operation_id, budget=budget)
    with session_factory() as db:
        return _public_state(db, operation_id)


def _claim(db, admin, approval_uuid, state_digest, digest, reason, ip) -> tuple[int, bool]:
    epoch = wallet_lots.require_enforced(db)
    approval = scope.find(db, admin, approval_uuid=approval_uuid, lock=True)
    existing = _by_approval(db, approval_uuid)
    if existing is not None:
        db.rollback()
        if existing["request_hash"] != digest:
            raise HTTPException(409, "receipt_void_request_changed")
        return int(existing["id"]), True
    scope.eligible(approval)
    rows = scope.effects(db, approval_uuid)
    resources = scope.scope(db, approval, rows)
    if scope.state_hash(db, approval, rows, resources) != state_digest:
        raise HTTPException(409, "preview_state_changed")
    scope.preflight(db, approval, rows, resources)
    connection_ids = [c["id"] for c in resources["connections"]] if resources["delete"] else []
    ctx = {"approval_uuid": approval_uuid, "shape": approval["target_shape"], "kind": approval["kind"],
           "user_id": resources["user_id"], "delete": resources["delete"], "connections": connection_ids,
           "customer_identity_id": scope.customer_identity(db, resources["user_id"]),
           "owner_admin_id_snapshot": approval["owner_admin_id_snapshot"], "actor_admin_id": admin.id,
           "username": approval["target_username_snapshot"], "forced": []}
    if ctx["customer_identity_id"] is not None:
        wallet_lots.fence_identity(db, ctx["customer_identity_id"])
    table, now = rv.wallet_operations, _now()
    try:
        with db.begin_nested():
            operation_id = int(db.execute(table.insert().values(
                operation_type=OPERATION_TYPE, business_key=approval_uuid, request_hash=digest,
                tenant_scope_key=approval["tenant_scope_key"], actor_kind="admin", actor_id=admin.id,
                actor_ip=(ip or None) and str(ip)[:64], reason=reason, state="running", epoch_at_start=epoch,
                result_snapshot=json.dumps({"context": ctx}, sort_keys=True), attempts=0, created_at=now,
                started_at=now, version=0)).inserted_primary_key[0])
    except IntegrityError:
        db.rollback()
        existing = _by_approval(db, approval_uuid)
        if existing is None or existing["request_hash"] != digest:
            raise HTTPException(409, "receipt_void_request_changed") from None
        return int(existing["id"]), True
    _approval_state(db, approval_uuid, ("completed",), "voiding", void_operation_id=operation_id)
    keys = step_keys(approval["target_shape"], connection_ids, resources["delete"] is not None)
    for order, step_key in enumerate(keys):
        db.execute(rv.wallet_operation_steps.insert().values(
            wallet_operation_id=operation_id, step_key=step_key, step_order=order,
            state="done" if step_key == "claim" else "pending", attempts=1 if step_key == "claim" else 0,
            result_snapshot="{}", created_at=now, completed_at=now if step_key == "claim" else None))
    db.commit()
    logger.info("receipt void operation %s claimed by admin %s", operation_id, admin.id)
    return operation_id, False


# ------------------------------------------------------------------ advance
def advance(session_factory, operation_id: int, *, budget: float = 15.0) -> str:
    """Run steps until the operation is terminal, blocked, waiting, or the
    budget is spent. Returns the operation state."""
    deadline = time.monotonic() + budget
    while True:
        with session_factory() as db:
            operation = _operation(db, operation_id)
            pending = next((s for s in _steps(db, operation_id) if s["state"] not in ("done", "abandoned")), None)
        if operation["state"] != "running":
            return operation["state"]
        if pending is None or (pending["next_retry_at"] is not None and pending["next_retry_at"] > _now()):
            return operation["state"]
        if pending["step_key"].startswith(DELETION_PREFIXES) or pending["step_key"] == "db_delete":
            progressed = _deletion(session_factory, operation, max(deadline - time.monotonic(), 1.0))
        else:
            progressed = _local(session_factory, operation, pending)
        if not progressed or time.monotonic() >= deadline:
            with session_factory() as db:
                return _operation(db, operation_id)["state"]


def _local(session_factory, operation, step) -> bool:
    ctx = context(operation)
    forced = frozenset(ctx.get("forced") or ())
    try:
        with session_factory() as db:
            wallet_lots.require_enforced(db, epoch=int(operation["epoch_at_start"]))
            locked = _operation(db, operation["id"], lock=True)
            _bump(db, locked)
            if ctx["customer_identity_id"] is not None:
                wallet_lots.fence_identity(db, ctx["customer_identity_id"])
            current = next(s for s in _steps(db, operation["id"]) if s["id"] == step["id"])
            if current["state"] in ("done", "abandoned"):
                db.rollback()
                return True
            name = step["step_key"]
            if name == "block":
                result = steps.block(db, ctx)
            elif name == "loyalty_reconciliation":
                result = steps.loyalty(db, ctx, operation["id"], forced)
            elif name == "wallet_reversal":
                result = steps.wallet(db, ctx, operation["id"])
            elif name == "ledger_reversal":
                result = steps.ledger(db, ctx, operation["id"], forced)
            elif name == "card_reversal":
                result = steps.card(db, ctx, operation["id"])
            elif name == "finalize":
                result = steps.finalize(db, ctx, operation["id"])
                _approval_state(db, ctx["approval_uuid"], ("voiding",), "voided", voided_at=_now())
            else:
                raise steps.StepFailed("receipt_void_step_unknown")
            _write_step(db, step["id"], state="done", attempts=current["attempts"] + 1, error_sanitized=None,
                        next_retry_at=None, result_snapshot=result, completed_at=_now())
            if name == "finalize":
                table = rv.wallet_operations
                db.execute(table.update().where(table.c.id == operation["id"]).values(
                    state="completed", completed_at=_now(), version=table.c.version + 1,
                    result_snapshot=json.dumps({"context": ctx, "result": "voided"}, sort_keys=True)))
            db.commit()
        return True
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the step, sanitized
        _fail(session_factory, operation["id"], step, exc)
        return False


def _fail(session_factory, operation_id, step, exc, *, blocked=False) -> None:
    code = _code(exc)
    if code == "receipt_void_operation_changed":
        return                                  # another worker ran this step; nothing failed
    logger.warning("receipt void %s step %s failed: %s", operation_id, step["step_key"], code)
    with session_factory() as db:
        operation = _operation(db, operation_id, lock=True)
        if operation["state"] != "running":
            db.rollback()
            return
        current = next(s for s in _steps(db, operation_id) if s["id"] == step["id"])
        attempts = int(current["attempts"]) + 1
        ctx = context(operation)
        if step["step_key"] == "block":
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code)
            _bump(db, operation, state="failed", completed_at=_now())
            _approval_state(db, ctx["approval_uuid"], ("voiding",), "completed", void_operation_id=None)
        elif blocked or isinstance(exc, void_effects.StepBlocked) or attempts >= MAX_ATTEMPTS:
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code, next_retry_at=None)
            _bump(db, operation, state="cleanup_required")
            _approval_state(db, ctx["approval_uuid"], ("voiding",), "cleanup_required")
        else:
            delay = BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)]
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code,
                        next_retry_at=_now() + dt.timedelta(seconds=delay))
            _bump(db, operation, attempts=rv.wallet_operations.c.attempts + 1)
        db.commit()


def _deletion(session_factory, operation, budget) -> bool:
    """Push the provisioning deletion once and mirror what it recorded onto
    the deprovision:/verify:/db_delete steps (one transaction)."""
    ctx = context(operation)
    first = next(s for s in _steps_of(session_factory, operation["id"]) if s["state"] not in ("done", "abandoned"))
    try:
        with session_factory() as db:
            if _held_debit(db, ctx):
                raise steps.StepFailed("wallet_account_has_hold")
        deletion.start_or_advance(session_factory, ctx, operation["id"], budget=budget)
    except Exception as exc:  # noqa: BLE001
        _fail(session_factory, operation["id"], first, exc,
              blocked=isinstance(exc, steps.StepFailed) and exc.code == "wallet_account_has_hold")
        return False
    with session_factory() as db:
        locked = _operation(db, operation["id"], lock=True)       # the locking read first (no stale snapshot)
        outcome = deletion.outcome(db, ctx, operation["id"])
        if locked["state"] != "running":
            db.rollback()
            return False
        _bump(db, locked)
        rows = {s["step_key"]: s for s in _steps(db, operation["id"])}
        now, blocked = _now(), False
        for cid in ctx["connections"]:
            seen = outcome["connections"].get(cid) or {"result": "running"}
            snapshot = {k: seen.get(k) for k in ("backend", "node_id", "protocol", "provisioning_step_id",
                                                 "remote_outcome", "remote_attempted", "forced_by_admin_id")}
            deprovision, verify = rows[f"deprovision:{cid}"], rows[f"verify:{cid}"]
            if seen["result"] == "absent":
                outcome_value = seen["remote_outcome"] if seen["remote_outcome"] in deletion.ABSENT else None
                for row in (deprovision, verify):
                    if row["state"] != "done":
                        _write_step(db, row["id"], state="done", remote_outcome=outcome_value if row is verify
                                    else None, error_sanitized=None, result_snapshot=snapshot, completed_at=now)
            elif seen["result"] == "abandoned":
                orphan = dict(snapshot, connection_id=cid, orphan_for_manual_cleanup=True)
                for row in (deprovision, verify):
                    if row["state"] != "abandoned":
                        _write_step(db, row["id"], state="abandoned", remote_outcome="abandoned",
                                    error_sanitized=None, result_snapshot=orphan, completed_at=now)
            elif seen["result"] == "unverified":
                blocked = True
                if deprovision["state"] != "done" and seen.get("remote_attempted"):
                    _write_step(db, deprovision["id"], state="done", result_snapshot=snapshot, completed_at=now)
                _write_step(db, verify["id"], state="blocked", remote_outcome="unverified",
                            error_sanitized=(seen.get("error") or "remote_unverified")[:80], result_snapshot=snapshot)
        if outcome["state"] == "completed":
            _write_step(db, rows["db_delete"]["id"], state="done", error_sanitized=None, completed_at=now,
                        result_snapshot={"provisioning_operation_id": outcome["provisioning_operation_id"],
                                         "finalizer": "delete_" + ctx["delete"]["kind"]})
        elif outcome["state"] == "cleanup_required" or blocked:
            if not blocked:
                _write_step(db, rows["db_delete"]["id"], state="failed",
                            error_sanitized=(outcome.get("error") or "provisioning_cleanup_required")[:80])
            _bump(db, dict(locked, version=locked["version"] + 1), state="cleanup_required")
            _approval_state(db, ctx["approval_uuid"], ("voiding",), "cleanup_required")
        db.commit()
        return outcome["state"] == "completed"


def _steps_of(session_factory, operation_id):
    with session_factory() as db:
        return _steps(db, operation_id)


def _held_debit(db, ctx) -> bool:
    if ctx["user_id"] is None:
        return False
    debits, accounts = rv.wallet_debit_events, rv.wallet_accounts
    return db.execute(select(debits.c.id).join(accounts, accounts.c.id == debits.c.wallet_account_id).where(
        accounts.c.user_id == ctx["user_id"], debits.c.state == "held").limit(1)).first() is not None
