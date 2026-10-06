"""Legacy-phase, idempotent customer wallet top-ups for A2.

This is deliberately limited to User.balance credits made by the bot
add-balance endpoint. Reseller balances, card-pool counters and debits do
not pass through this module. P9 replaces this legacy guard with the
wallet-credit-source unique key from the frozen Receipt Void design.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models
from . import accounting, bot_resources, receipt_approval_effects, receipt_approval_registration as registration
from . import receipt_void_schema

log = logging.getLogger(__name__)
ATTEMPTS = 3


def _tables():
    from .. import models_receipt_void as rv
    return rv.wallet_runtime_state, rv.receipt_approval_runtime_state, rv.receipt_approvals


def _start(db: Session) -> bool:
    """End auth-dependency work; SQLite must start as a writer."""
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
        return True
    return False


def _lock_wallet_phase(db: Session, sqlite: bool) -> None:
    wallet_state, _approval_state, _approvals = _tables()
    query = select(wallet_state.c.phase).where(wallet_state.c.id == 1)
    if not sqlite:
        query = query.with_for_update(read=True)
    phase = db.execute(query).scalar()
    if phase != "normal":
        raise HTTPException(status_code=503, detail="wallet_legacy_writer_unavailable")


def _lock_approval_mode(db: Session):
    from . import receipt_approval_runtime as runtime
    state = runtime.read_state(db, lock=True, shared_lock=True)
    if state is None:
        raise HTTPException(status_code=503, detail="receipt_approval_runtime_unavailable")
    mode = runtime.effective_registration_mode(state)
    if mode == runtime.BLOCKED:
        raise HTTPException(status_code=503, detail="receipt_approval_blocked")
    return state, mode


def _approval_row(db: Session, approval_uuid: str):
    _wallet_state, _approval_state, approvals = _tables()
    return db.execute(select(approvals).where(
        approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().first()


def _topup_ledger(db: Session, approval_uuid: str):
    return db.query(models.LedgerEntry).filter(
        models.LedgerEntry.approval_uuid == approval_uuid,
        models.LedgerEntry.kind == "wallet_topup",
    ).order_by(models.LedgerEntry.id).with_for_update().first()


def _ledger_from_effect(db: Session, approval_uuid: str):
    """Find the committed top-up through its effect even if an older writer
    failed to leave LedgerEntry.approval_uuid populated. Effect rows are
    mutation evidence too; they must never be mistaken for permission to
    issue another credit."""
    _wallet_state, _approval_state, _approvals = _tables()
    effects = receipt_approval_effects._tables()[2]
    effect = db.execute(select(effects.c.resource_id).where(
        effects.c.approval_uuid == approval_uuid,
        effects.c.effect_type == "ledger_topup",
        effects.c.effect_key == "topup",
        effects.c.resource_type == "LedgerEntry",
    ).with_for_update()).first()
    if effect is None or effect[0] is None:
        return None
    return db.query(models.LedgerEntry).filter(
        models.LedgerEntry.id == effect[0],
        models.LedgerEntry.kind == "wallet_topup",
    ).with_for_update().first()


def _pending_event(db: Session, row, approval_uuid: Optional[str], code: str) -> None:
    from . import receipt_approval_runtime as runtime
    if row is not None:
        source, local_id = row["pending_source_instance_id"], row["pending_local_id"]
    else:
        # The existing shadow-event schema requires a pending key. For an
        # unknown UUID use a deterministic non-PII synthetic key; do not
        # attach the untrusted UUID to the event row.
        digest = hashlib.sha256(str(approval_uuid or "").encode("utf-8")).digest()
        source = "a2-unknown-topup"
        local_id = int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)
    runtime.record_shadow_event(db, pending_source_instance_id=str(source), pending_local_id=int(local_id),
                                stage="effect", error_code=code, approval_uuid=approval_uuid if row is not None else None)


def _transient(exc: OperationalError, dialect: str) -> bool:
    original = getattr(exc, "orig", None)
    args = getattr(original, "args", ())
    code = args[0] if args else None
    message = str(original or exc).lower()
    if dialect == "sqlite":
        return isinstance(original, sqlite3.OperationalError) and any(word in message for word in ("locked", "busy"))
    return code in {1020, 1205, 1213}


def _credit(db: Session, user: models.User, amount: int, payment_card_id: Optional[int],
            approval_uuid: Optional[str], matched_approval: bool) -> models.LedgerEntry:
    """Atomic increment + ledger insert in the caller's transaction."""
    if matched_approval:
        # Record the state transition before the financial mutation, but in
        # the same transaction. Shadow effect failures remain best-effort.
        receipt_approval_effects.ShadowRecorder(db, approval_uuid).begin()
    result = db.execute(models.User.__table__.update().where(
        models.User.id == user.id).values(
            balance=func.coalesce(models.User.balance, 0) + amount))
    if result.rowcount != 1:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد")
    balance_after = int(db.execute(select(models.User.balance).where(
        models.User.id == user.id).with_for_update()).scalar() or 0)
    entry = accounting.record(db, "wallet_topup", amount, user=user,
                              admin_id=user.owner_admin_id, payment_card_id=payment_card_id,
                              payment_method="card")
    if matched_approval:
        entry.approval_uuid = approval_uuid
        db.flush()
        recorder = receipt_approval_effects.ShadowRecorder(db, approval_uuid)
        recorder.effect("wallet_credit_source_created", "credit:receipt_topup", user,
                        receipt_approval_effects.WalletCreditEvidence(balance_after - amount, balance_after))
        recorder.effect("ledger_topup", "topup", entry)
    return entry


def _run_once(db: Session, principal, username: str, amount: int, payment_card_id: Optional[int],
              approval_uuid: Optional[str], ensure_can_buy: Callable[[models.User], None]):
    sqlite = _start(db)
    # A top-up without an approval UUID is an ordinary balance credit (also
    # used by compensating refunds). It has no receipt identity to dedupe and
    # must keep working before the wallet runtime schema is available. SQLite
    # is still serialized by BEGIN IMMEDIATE; the UPDATE itself is atomic on
    # MariaDB. A receipt-correlated top-up must take the full L1/L2 chain.
    if approval_uuid:
        if not receipt_void_schema.is_ready():
            raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
        _lock_wallet_phase(db, sqlite)                     # L1
    approval_state = None
    effective_mode = "off"
    if approval_uuid:
        from . import receipt_approval_runtime as runtime
        approval_state, effective_mode = _lock_approval_mode(db)   # L2; blocked before lookup

    row = _approval_row(db, approval_uuid) if approval_uuid and receipt_void_schema.is_ready() else None  # L3
    # The approval row (L3) is already locked; acquire the target's row lock
    # as part of its scoped lookup so this is the single L4 locking read.
    user = bot_resources._get_user_or_403(db, principal, username, None, for_update=bool(approval_uuid))
    ensure_can_buy(user)
    if payment_card_id is not None:
        bot_resources._get_payment_card_or_403(db, principal, payment_card_id)

    if not approval_uuid:
        _credit(db, user, amount, payment_card_id, None, False)
        db.commit()
        db.refresh(user)
        return user

    if row is None:
        if effective_mode in ("required", "blocked"):
            raise HTTPException(status_code=422, detail="approval_not_found")
        _pending_event(db, None, approval_uuid, "approval_unknown")
        _credit(db, user, amount, payment_card_id, None, False)
        db.commit()
        db.refresh(user)
        return user

    row = dict(row)
    tagged_topup = _topup_ledger(db, approval_uuid)   # current locking read under L3
    effect_topup = _ledger_from_effect(db, approval_uuid)
    applied_topup = tagged_topup or effect_topup
    mutation_exists = registration.has_mutation_evidence(db, approval_uuid)
    mismatch = None
    try:
        key_instance = registration.caller_key_instance(db, principal)
    except registration.RegistrationRejected:
        key_instance = None
        mismatch = "approval_principal_mismatch"
    if mismatch is None and key_instance != row["execution_key_instance_uuid"]:
        mismatch = "approval_principal_mismatch"
    if mismatch is None and row["kind"] != "topup":
        mismatch = "approval_kind_mismatch"
    if mismatch is None and row["target_user_id_snapshot"] != user.id:
        mismatch = "approval_target_mismatch"
    if mismatch is None and int(row["amount_snapshot"]) != amount:
        mismatch = "approval_amount_mismatch"
    if mismatch is None and row["payment_card_id_snapshot"] != payment_card_id:
        mismatch = "approval_card_mismatch"

    strict = row["registered_under_mode"] == "required"
    if strict and not all(registration.runtime.REQUIRED_PREREQUISITES.values()):
        raise HTTPException(status_code=409, detail="execution_token_not_supported")

    # A matching customer+amount already credited by this approval dominates
    # a rotated key/card retry: do not pay twice, but keep the mismatch visible.
    if applied_topup is not None and applied_topup.user_id == user.id and int(applied_topup.amount) == amount:
        if row["state"] == "failed":
            registration.recover_failed_with_mutation(db, row)
        if mismatch:
            _pending_event(db, row, approval_uuid, mismatch)
        db.commit()
        db.refresh(user)
        return user

    terminal = row["state"] in ("cancelled", "voiding", "voided", "cleanup_required")
    if terminal:
        if strict:
            raise HTTPException(status_code=409, detail="approval_not_executable")
        if mutation_exists:
            _pending_event(db, row, approval_uuid, "topup_already_applied")
            db.commit()                                    # keep the log line; apply() rolls back on raise
            raise HTTPException(status_code=409, detail="approval_has_effects")
        _pending_event(db, row, approval_uuid, "approval_state_" + row["state"])
        _credit(db, user, amount, payment_card_id, None, False)
        db.commit()
        db.refresh(user)
        return user

    if mismatch:
        if strict:
            status = 403 if mismatch == "approval_principal_mismatch" else (409 if mismatch in (
                "approval_target_mismatch", "approval_card_mismatch") else 422)
            raise HTTPException(status_code=status, detail=mismatch)
        _pending_event(db, row, approval_uuid, mismatch)
        _credit(db, user, amount, payment_card_id, None, False)
        db.commit()
        db.refresh(user)
        return user

    if row["state"] == "failed":
        if mutation_exists:
            registration.recover_failed_with_mutation(db, row)
            _pending_event(db, row, approval_uuid, "topup_mutation_evidence")
            db.commit()
            raise HTTPException(status_code=409, detail="approval_has_effects")
        current_key = registration.caller_key_instance(db, principal)
        approver = None
        if row["approval_mode"] == registration.MANUAL:
            approver = registration.resolve_approver(
                db, row["approved_by_telegram_id"], user.owner_admin_id, payment_card_id, principal)
        row = registration._change_authority(
            db, row, reason="retry", to_mode=row["approval_mode"], approver=approver, to_key=current_key)

    # Any prior effect or tagged ledger is mutation evidence. The exact
    # matching top-up was handled by the L-dominance branch above. If the
    # evidence cannot be tied to this same customer+amount, fail closed:
    # never guess that another credit is safe.
    if mutation_exists:
        _pending_event(db, row, approval_uuid, "topup_mutation_evidence")
        db.commit()
        raise HTTPException(status_code=409, detail="approval_has_effects")

    _credit(db, user, amount, payment_card_id, approval_uuid, True)
    db.commit()
    db.refresh(user)
    return user


def apply(db: Session, principal, username: str, amount: int, payment_card_id: Optional[int],
          approval_uuid: Optional[str], ensure_can_buy: Callable[[models.User], None]) -> models.User:
    """Retry the complete transaction on known transient lock conflicts."""
    dialect = db.get_bind().dialect.name
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _run_once(db, principal, username, amount, payment_card_id, approval_uuid, ensure_can_buy)
        except OperationalError as exc:
            db.rollback()
            if not _transient(exc, dialect):
                raise
            if attempt == ATTEMPTS:
                raise HTTPException(status_code=503, detail="database_busy") from exc
            time.sleep(0.1 * attempt)
        except Exception:
            db.rollback()
            raise
    raise HTTPException(status_code=503, detail="database_busy")
