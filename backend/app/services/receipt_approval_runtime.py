"""Requested and EFFECTIVE mode of receipt-approval registration (Receipt
Void design 5.5 and 5.6; rollout phase P3).

The operator asks for a mode (off / shadow / required); what actually
applies is derived from that and from key-identity readiness, read from the
durable singleton row on every request - never from process memory:

    off                      -> off
    shadow,   keys ready     -> shadow
    shadow,   keys NOT ready -> shadow, every registration is a shadow error
    required, all prerequisites ready -> required
    required, any prerequisite missing -> blocked (never silently relaxed)

In this phase only off <-> shadow can be requested; 'required' exists only
through the joint activation of phase P9a.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models
from . import receipt_void_schema

log = logging.getLogger(__name__)

OFF, SHADOW, REQUIRED, BLOCKED, ENFORCED = "off", "shadow", "required", "blocked", "enforced"
SHADOW_ERROR_KEY_IDENTITY = "key_identity_not_ready"

# Receipt Void requires every production call carrying an approval UUID to
# present and validate the current execution token. P6 wired it through every
# approval-carrying endpoint: create_user / purchase_package / renew_service
# (and renew) run only on the durable path under 'required' (T1 binds token,
# key and manifest; no token -> 422, old token -> 409), every legacy path
# refuses 'required' (execution_token_not_supported), the bot's separate
# referral / discount / card calls answer from the durable operation, and a
# 'required' top-up needs the token too. 'required' itself still cannot be
# requested before the joint activation (P9a) and p6_ready().
REQUIRED_PREREQUISITES = {"execution_token_validation": True}


class ModeChangeRejected(Exception):
    """A requested mode change that the contract forbids; code is the
    stable error code of design section 18."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _table():
    from .. import models_receipt_void as rv
    return rv.receipt_approval_runtime_state


def _events():
    from .. import models_receipt_void as rv
    return rv.receipt_approval_shadow_events


def read_state(db: Session, *, lock: bool = False, shared_lock: bool = False) -> Optional[dict]:
    """The singleton row (None if the schema is not ready or the row cannot
    be read - callers treat that as 'off' for a request that asked for
    nothing, and as blocked wherever 'required' could apply)."""
    if not receipt_void_schema.is_ready():
        return None
    table = _table()
    query = select(table).where(table.c.id == receipt_void_schema.SINGLETON_ID)
    if lock:
        query = query.with_for_update(read=shared_lock)
    row = db.execute(query).mappings().first()
    return dict(row) if row else None


def effective_registration_mode(state: Optional[dict]) -> str:
    if state is None:
        return OFF
    requested = state["registration_mode"]
    if requested == REQUIRED and (
        not state["key_identity_ready"]
        or not all(REQUIRED_PREREQUISITES.values())
    ):
        return BLOCKED
    return requested


def _check_mutation_mode(db, approval_uuid, execution_token, *, sqlite, locking, allow_required=False):
    state = read_state(db, lock=locking and not sqlite, shared_lock=locking and not sqlite)
    if state is None:
        raise HTTPException(status_code=503, detail="receipt_approval_runtime_unavailable")
    mode = effective_registration_mode(state)
    if mode == BLOCKED:
        raise HTTPException(status_code=503, detail="receipt_approval_blocked")
    if mode == REQUIRED and not allow_required:
        raise HTTPException(status_code=503, detail="execution_token_not_supported")
    if execution_token is not None:
        from .. import models_receipt_void as rv
        query = select(rv.receipt_approvals.c.version).where(rv.receipt_approvals.c.approval_uuid == approval_uuid)
        if locking and not sqlite:
            query = query.with_for_update(read=True)
        if db.scalar(query) != execution_token:
            raise HTTPException(status_code=409, detail="approval_superseded")


def guard_approval_mutation(db: Session, approval_uuid: Optional[str],
                            execution_token: Optional[int] = None, *, keep_transaction: bool = False,
                            allow_required: bool = False) -> None:
    """Fail closed before any endpoint lookup/mutation when a correlated
    operation reaches a blocked or not-yet-supported required mode.

    Wallet top-ups use their stronger L1 -> L2 -> L3 -> L4 transaction in
    receipt_approval_topup. Other approval-carrying mutations need only L2
    here: rollback dependency reads first, then the durable runtime read.
    No approval, user, card, or ledger row is queried before the blocked
    decision.

    MariaDB keeps the shared row lock until the endpoint's first commit; it
    blocks nothing but a mode change. SQLite has one writer for the whole
    database, and these endpoints may talk to a router before they commit
    (a renewal re-enabling an expired customer's connections), so there the
    decision is a plain read of committed state and the transaction is
    ended before returning - holding the writer would stall RADIUS
    accounting and the quota poller behind a router call.
    """
    if not approval_uuid:
        if execution_token is not None:
            raise HTTPException(status_code=422, detail="approval_token_without_uuid")
        return
    if execution_token is not None and (type(execution_token) is not int or execution_token < 0):
        raise HTTPException(status_code=422, detail="approval_execution_token_invalid")
    if not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    if keep_transaction:
        # The caller already began a short wallet-writer transaction (L1,
        # wallet_service.begin_writer) and commits it itself: check INSIDE it,
        # keeping every lock (and on SQLite the writer) until that commit, so
        # a concurrent switch to 'blocked' cannot slip between check and write.
        _check_mutation_mode(db, approval_uuid, execution_token, sqlite=db.get_bind().dialect.name == "sqlite",
                             locking=True, allow_required=allow_required)
        return
    for attempt in range(1, 4):
        db.rollback()
        try:
            sqlite = db.get_bind().dialect.name == "sqlite"
            state = read_state(db, lock=not sqlite, shared_lock=not sqlite)
            if state is None:
                db.rollback()
                raise HTTPException(status_code=503, detail="receipt_approval_runtime_unavailable")
            mode = effective_registration_mode(state)
            if mode == BLOCKED:
                db.rollback()
                raise HTTPException(status_code=503, detail="receipt_approval_blocked")
            if mode == REQUIRED and not allow_required:
                # Only the durable path executes under 'required': it binds
                # the token, key and manifest in T1 (Lifecycle 7.6).
                db.rollback()
                raise HTTPException(status_code=503, detail="execution_token_not_supported")
            if execution_token is not None:
                from .. import models_receipt_void as rv
                query = select(rv.receipt_approvals.c.version).where(
                    rv.receipt_approvals.c.approval_uuid == approval_uuid)
                if not sqlite:
                    query = query.with_for_update(read=True)
                current = db.scalar(query)
                if current != execution_token:
                    db.rollback()
                    raise HTTPException(status_code=409, detail="approval_superseded")
            if sqlite:
                db.rollback()
            return
        except HTTPException:
            raise
        except OperationalError as exc:
            db.rollback()
            if attempt == 3:
                raise HTTPException(status_code=503, detail="database_busy") from exc
            time.sleep(0.2 * attempt)
        except Exception:
            db.rollback()
            raise


def shadow_error(state: Optional[dict]) -> Optional[str]:
    """The reason every registration fails while in shadow, if any."""
    if state is not None and state["registration_mode"] == SHADOW and not state["key_identity_ready"]:
        return SHADOW_ERROR_KEY_IDENTITY
    return None


def effective_auto_rate_limit_mode(state: Optional[dict]) -> str:
    if state is None:
        return OFF
    requested = state["auto_rate_limit_mode"]
    if requested == ENFORCED and effective_registration_mode(state) != REQUIRED:
        return OFF          # blocked: no grant exists that could be limited
    if requested == SHADOW and effective_registration_mode(state) == OFF:
        return OFF
    return requested


def describe(db: Session) -> dict:
    """Body of GET .../runtime-mode."""
    state = read_state(db)
    return {
        "schema_ready": state is not None,
        "requested_registration_mode": state["registration_mode"] if state else OFF,
        "effective_registration_mode": effective_registration_mode(state),
        "requested_auto_rate_limit_mode": state["auto_rate_limit_mode"] if state else OFF,
        "effective_auto_rate_limit_mode": effective_auto_rate_limit_mode(state),
        "key_identity_ready": bool(state and state["key_identity_ready"]),
        "key_identity_failure": state["key_identity_failure"] if state else "schema_not_ready",
        "key_identity_failed_at": state["key_identity_failed_at"] if state else None,
        "completeness_generation": state["completeness_generation"] if state else 0,
        "loyalty_timing_mode": state["loyalty_timing_mode"] if state else "legacy_activation",
    }


def set_requested_mode(db: Session, *, registration_mode: Optional[str] = None,
                       auto_rate_limit_mode: Optional[str] = None) -> dict:
    """PUT .../runtime-mode: off <-> shadow only, in the caller's
    transaction. Raises ModeChangeRejected with the contract's code."""
    receipt_void_schema.assert_ready()
    # Same global order as activation and HA changes: wallet -> runtime ->
    # panel. SQLite acquires its writer before reading either singleton.
    from .. import models_receipt_void as rv
    wallet = rv.wallet_runtime_state
    if db.get_bind().dialect.name == "sqlite":
        db.execute(wallet.update().where(wallet.c.id == 1).values(epoch=wallet.c.epoch))
    else:
        db.execute(select(wallet.c.id).where(wallet.c.id == 1).with_for_update(read=True)).one()
    state = read_state(db, lock=True)
    if state is None:
        raise ModeChangeRejected("runtime_state_missing")
    if registration_mode == REQUIRED:
        raise ModeChangeRejected("use_joint_activation")
    if registration_mode is not None and registration_mode not in (OFF, SHADOW):
        raise ModeChangeRejected("invalid_registration_mode")
    if auto_rate_limit_mode is not None and auto_rate_limit_mode not in (OFF, SHADOW, ENFORCED):
        raise ModeChangeRejected("invalid_auto_rate_limit_mode")
    if state["registration_mode"] == REQUIRED and registration_mode is not None:
        raise ModeChangeRejected("use_deactivate")          # leaving 'required' has its own endpoint
    new_registration = registration_mode if registration_mode is not None else state["registration_mode"]
    new_rate = auto_rate_limit_mode if auto_rate_limit_mode is not None else state["auto_rate_limit_mode"]
    if new_rate == ENFORCED and new_registration != REQUIRED:
        raise ModeChangeRejected("enforced_requires_required")
    if new_rate == ENFORCED:
        if effective_registration_mode(state) != REQUIRED:
            raise ModeChangeRejected("key_identity_not_ready")
        panel = db.execute(select(models.PanelSettings.ha_enabled).where(
            models.PanelSettings.id == 1).with_for_update(read=True)).scalar_one_or_none()
        if panel:
            # No external coordinator is implemented; do not accept a
            # string claiming one as proof of distributed fencing.
            raise ModeChangeRejected("ha_requires_external_fencing")
        if state["auto_rate_limit_mode"] != ENFORCED:
            from . import receipt_approval_auto
            receipt_approval_auto.seed_recent(db, receipt_approval_auto.database_now(db))
    if new_registration == OFF and new_rate != OFF:
        if auto_rate_limit_mode is not None:
            raise ModeChangeRejected("rate_limit_requires_registration")
        new_rate = OFF                                       # turning registration off turns its limiter off
    table = _table()
    db.execute(table.update().where(table.c.id == state["id"]).values(
        registration_mode=new_registration, auto_rate_limit_mode=new_rate,
        updated_at=dt.datetime.utcnow(), version=table.c.version + 1))
    log.warning("receipt approval runtime mode: registration %s -> %s, auto rate limit %s -> %s",
                state["registration_mode"], new_registration, state["auto_rate_limit_mode"], new_rate)
    return {"registration_mode": new_registration, "auto_rate_limit_mode": new_rate}


def record_shadow_event(db: Session, *, pending_source_instance_id: str, pending_local_id: int, stage: str,
                        error_code: str, approval_uuid: Optional[str] = None) -> None:
    """One immutable log row for a shadow failure - no username, no amount.
    In its own savepoint; never raises (a log that cannot be written must
    not break the approval it is describing)."""
    try:
        with db.begin_nested():
            db.execute(_events().insert().values(
                pending_source_instance_id=str(pending_source_instance_id)[:64], pending_local_id=int(pending_local_id),
                stage=stage, error_code=str(error_code)[:48], approval_uuid=approval_uuid,
                created_at=dt.datetime.utcnow()))
    except Exception:
        log.exception("receipt approval shadow event could not be written (%s/%s)", stage, error_code)


def canonical_json(value) -> str:
    """Sorted keys, no whitespace, integers only (design 5.3) - the one
    serialisation every hash and every expected/actual comparison uses."""
    def reject_floats(item):
        if isinstance(item, float):
            raise TypeError("canonical JSON has no floats")
        if isinstance(item, dict):
            for key, inner in item.items():
                if not isinstance(key, str):
                    raise TypeError("canonical JSON keys are strings")
                reject_floats(inner)
        elif isinstance(item, (list, tuple)):
            for inner in item:
                reject_floats(inner)
    reject_floats(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
