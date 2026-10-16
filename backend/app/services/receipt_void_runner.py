"""Running a claimed receipt void (Receipt Void 8, 12.1, 13.2, 13.3).

Lease plan (12.1, 8): before its first local step a run resolves every key
the void touches (receipt_void_scope.lease_keys: the customer identity,
every identity a caused source credited, the receipt's card pool), sorted,
acquires them all as one owner op:{operation_id}:{random} in one committed
transaction (any busy -> nothing held, the run waits for the next pass - a
live lease is never taken over), then re-resolves under them (a different
set -> release, start again). Every step transaction then:

    begin_business (SQLite: BEGIN IMMEDIATE) -> phase read (epoch of the
    operation) -> approval row -> operation row (version CAS) -> revalidate
    the WHOLE lease set (owner + epoch + DB-clock liveness, FOR UPDATE) and
    renew it -> the key set re-resolved must still equal it -> the step.

The set is released only after the last step transaction of the run
committed or rolled back - and BEFORE steps 3 - 5, which are the
provisioning deletion machine with its own lease on the identity (never two
holders; never a remote call inside a transaction of this module). After the
deletion a run acquires the set again for the financial steps.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import secrets
import time

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .. import models_receipt_void as rv
from . import receipt_void_deletion as deletion, receipt_void_effects as void_effects
from . import receipt_void_engine as engine, receipt_void_scope as scope, receipt_void_steps as steps
from . import resource_leases, wallet_lots
from .receipt_void_engine import _approval_state, _bump, _code, _lock_approval, _now, _operation, _steps, _write_step

logger = logging.getLogger(__name__)
LEASE_TTL = 120
RESOLVE_ATTEMPTS = 3
TRANSIENT_ATTEMPTS = 4


def transient(exc) -> bool:
    """MariaDB's answer to a lock conflict - snapshot changed under a locking
    read (1020), deadlock (1213), lock wait timeout (1205): the transaction
    was rolled back as a whole, so running the whole step again is safe."""
    code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
    return isinstance(exc, OperationalError) and code in (1020, 1205, 1213)


class LeaseScopeChanged(Exception):
    """The keys re-resolved under the leases are not the leased set."""


# ------------------------------------------------------------------ leases (12.1, 8.1, 8.2)
def _keys(db, ctx) -> list[str]:
    return scope.lease_keys(db, ctx["approval_uuid"], ctx["customer_identity_id"])


def acquire(session_factory, operation: dict) -> tuple:
    ctx = engine.context(operation)
    for _attempt in range(RESOLVE_ATTEMPTS):
        with session_factory() as db:
            keys = _keys(db, ctx)                                     # 1. resolve, 2. sorted
        if not keys:
            return ()
        owner = f"op:{operation['id']}:{secrets.randbits(62) + 1}"
        with session_factory() as db:
            resource_leases.begin_business(db)
            try:
                leases = tuple(resource_leases.acquire(db, key, owner, ttl=LEASE_TTL) for key in keys)   # 3.
                db.commit()
            except BaseException:
                db.rollback()                                         # busy: nothing of the set is held
                raise
        with session_factory() as db:
            again = _keys(db, ctx)                                    # 4. re-resolve under the leases
        if again == keys:
            return leases
        release(session_factory, leases)
    raise HTTPException(409, "receipt_void_lease_scope_unstable")


def release(session_factory, leases) -> None:
    if not leases:
        return
    with session_factory() as db:
        for lease in leases:
            resource_leases.release(db, lease)
        db.commit()


def _fence(db, ctx, leases) -> None:
    """8.3 + 12.1 step 5, inside a business transaction."""
    if not leases:
        return
    resource_leases.revalidate(db, leases)
    for lease in leases:
        resource_leases.renew(db, lease, ttl=LEASE_TTL)
    if _keys(db, ctx) != [lease.resource_key for lease in leases]:
        raise LeaseScopeChanged()


def leased_identities(leases) -> set:
    return {int(l.resource_key.split(":", 1)[1]) for l in leases if l.resource_key.startswith("customer_identity:")}


# ------------------------------------------------------------------ advance
def advance(session_factory, operation_id: int, *, budget: float = 15.0) -> str:
    """Run steps until the operation is terminal, blocked, waiting, its lease
    set is busy, or the budget is spent. Returns the operation state."""
    deadline = time.monotonic() + budget
    leases = None
    try:
        while True:
            with session_factory() as db:
                operation = _operation(db, operation_id)
                pending = next((s for s in _steps(db, operation_id) if s["state"] not in ("done", "abandoned")), None)
            if operation["state"] != "running" or pending is None or (
                    pending["next_retry_at"] is not None and pending["next_retry_at"] > _now()):
                return operation["state"]
            if pending["step_key"].startswith(engine.DELETION_PREFIXES) or pending["step_key"] == "db_delete":
                release(session_factory, leases)
                leases = None
                progressed = _deletion(session_factory, operation, max(deadline - time.monotonic(), 1.0))
            else:
                if leases is None:
                    try:
                        leases = acquire(session_factory, operation)
                    except resource_leases.LeaseBusy:
                        logger.info("receipt void %s: lease set busy, waiting", operation_id)
                        return operation["state"]
                progressed = _local(session_factory, operation, pending, leases)
                if progressed == "lease":
                    release(session_factory, leases)
                    leases = None
                    progressed = False
            if not progressed or time.monotonic() >= deadline:
                with session_factory() as db:
                    return _operation(db, operation_id)["state"]
    finally:
        release(session_factory, leases)


def _local(session_factory, operation, step, leases):
    for attempt in range(1, TRANSIENT_ATTEMPTS + 1):
        try:
            return _local_once(session_factory, operation, step, leases)
        except OperationalError as exc:
            if not transient(exc) or attempt == TRANSIENT_ATTEMPTS:
                _fail(session_factory, operation["id"], step, exc)
                return False
            logger.info("receipt void %s step %s: transient lock conflict, again", operation["id"], step["step_key"])
            with session_factory() as db:
                operation = _operation(db, operation["id"])


def _local_once(session_factory, operation, step, leases):
    ctx = engine.context(operation)
    forced = frozenset(ctx.get("forced") or ())
    reconcile = []
    try:
        with session_factory() as db:
            resource_leases.begin_business(db)
            wallet_lots.require_enforced(db, epoch=int(operation["epoch_at_start"]))
            _lock_approval(db, ctx["approval_uuid"])
            locked = _operation(db, operation["id"], lock=True)
            _bump(db, locked)
            _fence(db, ctx, leases)
            current = next(s for s in _steps(db, operation["id"]) if s["id"] == step["id"])
            if current["state"] in ("done", "abandoned"):
                db.rollback()
                return True
            name = step["step_key"]
            if name == "block":
                # The claim committed before the lease set existed and before
                # the fraud hold: a purchase, renewal or wallet movement may
                # have landed in between. Under the full lease set, before ANY
                # mutation, the scope must still be exactly the confirmed one;
                # otherwise block fails (nothing applied, approval completed)
                # and only a fresh preview re-plans this operation.
                engine.revalidate_scope(db, locked)
                result = steps.block(db, ctx)
            elif name == "loyalty_reconciliation":
                result = steps.loyalty(db, ctx, operation["id"], forced)
            elif name == "wallet_reversal":
                result = steps.wallet(db, ctx, operation["id"], leased_identities(leases))
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
            reconcile = list(db.info.pop(void_effects.RECONCILE, []))
            db.commit()
        void_effects.reconcile_after_commit(session_factory, reconcile)
        return True
    except (LeaseScopeChanged, resource_leases.LeaseLost):
        logger.info("receipt void %s: lease set changed or lost before %s; re-acquiring", operation["id"], step["step_key"])
        return "lease"
    except OperationalError as exc:
        if transient(exc):
            raise                                   # _local runs the whole step again
        _fail(session_factory, operation["id"], step, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - every failure is recorded on the step, sanitized
        _fail(session_factory, operation["id"], step, exc)
        return False


def _fail(session_factory, operation_id, step, exc, *, blocked=False) -> None:
    code = _code(exc)
    if code == "receipt_void_operation_changed":
        return                                  # another worker ran this step; nothing failed
    logger.warning("receipt void %s step %s failed: %s", operation_id, step["step_key"], code)
    with session_factory() as db:
        approval_uuid = _operation(db, operation_id)["business_key"]
    with session_factory() as db:
        _lock_approval(db, approval_uuid)                     # locking reads first, approval before operation
        operation = _operation(db, operation_id, lock=True)
        if operation["state"] != "running":
            db.rollback()
            return
        current = next(s for s in _steps(db, operation_id) if s["id"] == step["id"])
        attempts = int(current["attempts"]) + 1
        ctx = engine.context(operation)
        if step["step_key"] == "block":
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code)
            _bump(db, operation, state="failed", completed_at=_now())
            _approval_state(db, ctx["approval_uuid"], ("voiding",), "completed", void_operation_id=None)
        elif blocked or isinstance(exc, void_effects.StepBlocked) or attempts >= engine.MAX_ATTEMPTS:
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code, next_retry_at=None)
            _bump(db, operation, state="cleanup_required")
            _approval_state(db, ctx["approval_uuid"], ("voiding",), "cleanup_required")
        else:
            delay = engine.BACKOFF_SECONDS[min(attempts - 1, len(engine.BACKOFF_SECONDS) - 1)]
            _write_step(db, step["id"], state="failed", attempts=attempts, error_sanitized=code,
                        next_retry_at=_now() + dt.timedelta(seconds=delay))
            _bump(db, operation, attempts=rv.wallet_operations.c.attempts + 1)
        db.commit()


# ------------------------------------------------------------------ steps 3 - 5
def _deletion(session_factory, operation, budget) -> bool:
    """Push the provisioning deletion once and mirror what it recorded onto
    the deprovision:/verify:/db_delete steps (one transaction)."""
    ctx = engine.context(operation)
    with session_factory() as db:
        first = next(s for s in _steps(db, operation["id"]) if s["state"] not in ("done", "abandoned"))
    try:
        with session_factory() as db:
            if _held_debit(db, ctx):
                raise steps.StepFailed("wallet_account_has_hold")
            if deletion.provisioning_operation(db, ctx["delete"], operation["id"]) is None and \
                    _scope_connections(db, ctx) != sorted(ctx["connections"]):
                # After block the account is held and disabled; a connection
                # still added by hand would be deleted without being in the
                # confirmed plan: stop before the deletion starts.
                raise steps.StepFailed("receipt_void_scope_changed")
        deletion.start_or_advance(session_factory, ctx, operation["id"], budget=budget)
    except Exception as exc:  # noqa: BLE001
        _fail(session_factory, operation["id"], first, exc,
              blocked=isinstance(exc, steps.StepFailed) and exc.code in (
                  "wallet_account_has_hold", "receipt_void_scope_changed"))
        return False
    with session_factory() as db:
        _lock_approval(db, ctx["approval_uuid"])
        locked = _operation(db, operation["id"], lock=True)       # the locking reads first (no stale snapshot)
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


def _held_debit(db, ctx) -> bool:
    if ctx["user_id"] is None:
        return False
    debits, accounts = rv.wallet_debit_events, rv.wallet_accounts
    return db.execute(select(debits.c.id).join(accounts, accounts.c.id == debits.c.wallet_account_id).where(
        accounts.c.user_id == ctx["user_id"], debits.c.state == "held").limit(1)).first() is not None


def _scope_connections(db, ctx) -> list:
    from .. import models
    query = db.query(models.Connection.id)
    if ctx["delete"]["kind"] == "user":
        query = query.filter(models.Connection.user_id == ctx["delete"]["id"])
    else:
        query = query.filter(models.Connection.purchase_id == ctx["delete"]["id"])
    return sorted(row.id for row in query)
