"""The durable receipt void (Receipt Void 13; P11):
wallet_operations(operation_type='void_receipt', business_key=approval_uuid)
and its wallet_operation_steps in the fixed order of 13.2.

  execute()     an exact replay (signed token of this approval, the same
                request hash) is answered with the operation it created -
                before any flag or expiry rule, which only apply to a NEW
                claim. A new claim is ONE transaction opened with
                begin_business: phase read, the flags under a shared lock
                (receipt_void_flags.lock_accepting - set_flag takes the row
                exclusively, so "execute off" / "worker off" serialize with
                it), the approval row, the full scope fingerprint against the
                preview's state hash, then the operation, approval
                completed -> voiding (CAS) and every step. A failed operation
                (block failed: nothing applied) is re-claimed only with a
                FRESH preview of the current scope.
  advance()     receipt_void_runner: the sorted durable lease set (12.1), one
                step per transaction, steps 3 - 5 on the provisioning
                deletion machine.
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
from . import resource_leases, wallet_lots

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


def _lock_approval(db, approval_uuid) -> Optional[dict]:
    """The approval row before the operation row (the owner-approved order:
    runtime -> approval -> operation -> leases -> targets)."""
    table = rv.receipt_approvals
    row = db.execute(wallet_lots._for_update(db, select(table).where(
        table.c.approval_uuid == approval_uuid)).execution_options(populate_existing=True)).mappings().first()
    return dict(row) if row else None


def _replay(existing: Optional[dict], token: dict, digest: str) -> Optional[dict]:
    """The operation an exact repeat of its request created (any actor)."""
    if existing is None or existing["business_key"] != token["u"] or existing["request_hash"] != digest:
        return None
    if existing["state"] == "failed" and token["h"] not in (context(existing).get("preview_hashes") or ()):
        return None                              # a fresh preview of a failed void: a new claim
    return {"operation_id": existing["id"], "state": existing["state"], "replayed": True}


def execute(session_factory, admin, *, preview_token, typed_username, reason, danger_ack, ip=None,
            budget: float = 15.0) -> dict:
    if danger_ack is not True:
        raise HTTPException(422, "danger_ack_required")
    reason = _reason(reason)
    if not isinstance(typed_username, str) or not typed_username:
        raise HTTPException(422, "typed_username_required")
    with session_factory() as db:
        receipt_void_schema_ready()
        token = scope.read_token(preview_token, check_expiry=False)       # signature + approval binding
        approval = scope.find(db, admin, approval_uuid=token["u"])        # authorization: 404 otherwise
        if typed_username != approval["target_username_snapshot"]:
            raise HTTPException(422, "typed_username_mismatch")
        digest = _request_hash(approval["approval_uuid"], typed_username, reason)
        existing = _by_approval(db, approval["approval_uuid"])
        replay = _replay(existing, token, digest)
        if replay is not None:
            return replay
        if existing is not None and existing["state"] != "failed":
            raise HTTPException(409, "receipt_void_request_changed")
        flags.require(db, "execute")                                      # only a NEW claim from here
        scope.read_token(preview_token)                                   # ... with an unexpired preview
        db.rollback()
        operation_id, replayed = _claim(db, admin, approval["approval_uuid"], token, digest, reason, ip)
    if replayed:
        with session_factory() as db:
            return dict(_public_state(db, operation_id), replayed=True)
    with session_factory() as db:
        worker = flags.enabled(db, "worker")
    if worker:
        from . import receipt_void_runner
        receipt_void_runner.advance(session_factory, operation_id, budget=budget)
    with session_factory() as db:
        return _public_state(db, operation_id)


def receipt_void_schema_ready():
    from . import receipt_void_schema
    receipt_void_schema.assert_ready()


def _context(db, admin, approval, rows, resources, fingerprint, token_hash) -> dict:
    connection_ids = [c["id"] for c in resources["connections"]] if resources["delete"] else []
    identity = resources["customer_identity_id"]
    return {"approval_uuid": approval["approval_uuid"], "shape": approval["target_shape"], "kind": approval["kind"],
            "user_id": resources["user_id"], "delete": resources["delete"], "connections": connection_ids,
            "customer_identity_id": identity, "owner_admin_id_snapshot": approval["owner_admin_id_snapshot"],
            "actor_admin_id": admin.id, "username": approval["target_username_snapshot"], "forced": [],
            "fingerprint": fingerprint, "preview_hashes": [token_hash],
            "lease_keys": scope.lease_keys(db, approval["approval_uuid"], identity)}


def _claim(db, admin, approval_uuid, token, digest, reason, ip) -> tuple[int, bool]:
    resource_leases.begin_business(db)
    try:
        epoch = wallet_lots.require_enforced(db)
        flags.lock_accepting(db)                       # execute + worker, held to the commit
        _lock_approval(db, approval_uuid)
        approval = scope.find(db, admin, approval_uuid=approval_uuid)
        existing = _by_approval(db, approval_uuid)
        replay = _replay(existing, token, digest)
        if replay is not None:
            db.rollback()
            return int(existing["id"]), True
        if existing is not None and existing["state"] != "failed":
            raise HTTPException(409, "receipt_void_request_changed")
        scope.eligible(approval)
        rows = scope.effects(db, approval_uuid)
        resources = scope.scope(db, approval, rows)
        fingerprint = scope.fingerprint(db, approval, rows, resources)
        if scope.state_hash(db, approval, rows, resources, scope_fingerprint=fingerprint) != token["h"]:
            raise HTTPException(409, "preview_state_changed")
        scope.preflight(db, approval, rows, resources)
        ctx = _context(db, admin, approval, rows, resources, fingerprint, token["h"])
        keys = step_keys(approval["target_shape"], ctx["connections"], resources["delete"] is not None)
        table, now = rv.wallet_operations, _now()
        values = dict(request_hash=digest, tenant_scope_key=approval["tenant_scope_key"], actor_kind="admin",
                      actor_id=admin.id, actor_ip=(ip or None) and str(ip)[:64], reason=reason, state="running",
                      epoch_at_start=epoch, completed_at=None, next_retry_at=None, started_at=now)
        if existing is not None:                      # a failed void (nothing applied), with a fresh preview
            ctx["preview_hashes"] = (context(existing).get("preview_hashes") or []) + [token["h"]]
            _bump(db, existing, result_snapshot=json.dumps({"context": ctx}, sort_keys=True), **values)
            operation_id = int(existing["id"])
            _rebuild_steps(db, operation_id, keys, now)
        else:
            try:
                with db.begin_nested():
                    operation_id = int(db.execute(table.insert().values(
                        operation_type=OPERATION_TYPE, business_key=approval_uuid, attempts=0, created_at=now,
                        version=0, result_snapshot=json.dumps({"context": ctx}, sort_keys=True),
                        **values)).inserted_primary_key[0])
            except IntegrityError:
                raise HTTPException(409, "receipt_void_request_changed") from None
            for order, step_key in enumerate(keys):
                db.execute(rv.wallet_operation_steps.insert().values(
                    wallet_operation_id=operation_id, step_key=step_key, step_order=order,
                    state="done" if step_key == "claim" else "pending", attempts=1 if step_key == "claim" else 0,
                    result_snapshot="{}", created_at=now, completed_at=now if step_key == "claim" else None))
        _approval_state(db, approval_uuid, ("completed",), "voiding", void_operation_id=operation_id)
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.info.pop("resource_lease_business_transaction", None)
    logger.info("receipt void operation %s claimed by admin %s", operation_id, admin.id)
    return operation_id, False


def _rebuild_steps(db, operation_id: int, keys: list, now) -> None:
    """A failed void re-claimed with a fresh preview: nothing past the claim
    ever committed (block is one transaction), so its steps are re-planned
    for the CURRENT confirmed scope. A step of a connection no longer in it is
    closed as superseded (never run); every step of the new plan is pending."""
    existing = {s["step_key"]: s for s in _steps(db, operation_id)}
    if any(s["state"] not in ("pending", "failed") for k, s in existing.items() if k != "claim"):
        raise HTTPException(409, "receipt_void_not_retryable")
    for key, row in existing.items():
        if key not in keys:
            _write_step(db, row["id"], state="done", step_order=1000 + row["step_order"], next_retry_at=None,
                        result_snapshot={"superseded_by_new_preview": True}, completed_at=now)
    for order, key in enumerate(keys):
        if key in existing:
            if key != "claim":
                _write_step(db, existing[key]["id"], step_order=order, state="pending", attempts=0,
                            error_sanitized=None, next_retry_at=None, remote_outcome=None)
        else:
            db.execute(rv.wallet_operation_steps.insert().values(
                wallet_operation_id=operation_id, step_key=key, step_order=order, state="pending", attempts=0,
                result_snapshot="{}", created_at=now))


def revalidate_scope(db, operation: dict) -> None:
    """The scope must still be exactly the one the operator confirmed
    (otherwise a fresh preview + execute is required): at block, under the
    void's full lease set before its first mutation, and on retry of a failed
    void."""
    ctx = context(operation)
    approval = _lock_approval(db, ctx["approval_uuid"])
    rows = scope.effects(db, ctx["approval_uuid"])
    resources = scope.scope(db, approval, rows)
    if scope.fingerprint(db, approval, rows, resources) != ctx.get("fingerprint"):
        raise HTTPException(409, "preview_state_changed")
