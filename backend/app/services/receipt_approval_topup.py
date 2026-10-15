"""Legacy-phase, idempotent customer wallet top-ups for A2.

This is deliberately limited to User.balance credits made by the bot
add-balance endpoint. Reseller balances, card-pool counters and debits do
not pass through this module. In the 'enforced' wallet phase (P9) the
credit is a receipt_topup credit source keyed by the approval (its unique
key is then the idempotency guard); a top-up with no matching approval is
refused there.
"""
from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import models
from . import accounting, bot_resources, receipt_approval_effects, receipt_approval_registration as registration
from . import wallet_service
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


def _lock_wallet_phase(db: Session, sqlite: bool) -> str:
    """L1: the phase decides the generation (legacy 'normal' / lots 'enforced')."""
    phase = wallet_service.writer_phase(db)
    db.info["wallet_topup_phase"] = phase
    return phase


def _lot_credit(db: Session, user: models.User, amount: int, approval_uuid: Optional[str]) -> int:
    """'enforced': a receipt top-up is one wallet_credit_source of origin
    receipt_topup caused by its approval (11.1, 12.2). There is no credit
    without an origin in this generation: a top-up not tied to a matching
    approval is refused, never booked as anonymous money."""
    from . import wallet_lots
    if not approval_uuid:
        raise HTTPException(status_code=422, detail="topup_requires_approval")
    account = wallet_lots.account_of_user(db, user.id)
    wallet_lots.fence_identity(db, account["customer_identity_id"])
    epoch = wallet_lots.require_enforced(db)
    operation_id, _ = wallet_lots.operation(
        db, "wallet_credit_apply", f"{account['id']}:receipt_topup:{approval_uuid}",
        payload={"origin": "receipt_topup", "approval_uuid": approval_uuid, "amount": amount},
        tenant_scope_key=wallet_lots.tenant_of(db, account), epoch=epoch, actor_kind="bot")
    source = wallet_lots.credit(db, account, origin_kind="receipt_topup", source_key=approval_uuid, amount=amount,
                                operation_id=operation_id, causation_approval_uuid=approval_uuid)
    wallet_lots.complete(db, operation_id, {"wallet_credit_source_id": source})
    db.info[wallet_service.LAST_CREDIT_SOURCE] = source
    return wallet_lots.available(db, account["id"])


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
    if db.info.get("wallet_topup_phase") == wallet_service.ENFORCED_PHASE:
        balance_after = _lot_credit(db, user, amount, approval_uuid if matched_approval else None)
    else:
        balance_after = wallet_service.credit_atomic(
            db, user.id, amount, phase_checked=True,
            source_kind=wallet_service.RECEIPT_TOPUP if matched_approval else wallet_service.UNSPECIFIED)
    if balance_after is None:
        raise HTTPException(status_code=404, detail="کاربر پیدا نشد")
    entry = accounting.record(db, "wallet_topup", amount, user=user,
                              admin_id=user.owner_admin_id, payment_card_id=payment_card_id,
                              payment_method="card")
    if matched_approval:
        entry.approval_uuid = approval_uuid
        db.flush()
        recorder = receipt_approval_effects.ShadowRecorder(db, approval_uuid)
        source = wallet_service.take_credit_source(db)
        if source is not None:              # lot generation: the effect is the credit source (part may settle debt)
            from . import wallet_lots
            evidence = wallet_lots.credit_evidence(db, source, user.id)
        else:
            evidence = receipt_approval_effects.WalletCreditEvidence(balance_after - amount, balance_after)
        recorder.effect("wallet_credit_source_created", "credit:receipt_topup", user, evidence)
        recorder.effect("ledger_topup", "topup", entry)
    return entry


def _run_once(db: Session, principal, username: str, amount: int, payment_card_id: Optional[int],
              approval_uuid: Optional[str], ensure_can_buy: Callable[[models.User], None],
              execution_token: Optional[int] = None):
    if not approval_uuid and execution_token is not None:
        raise HTTPException(status_code=422, detail="approval_token_without_uuid")
    if execution_token is not None and (type(execution_token) is not int or execution_token < 0):
        raise HTTPException(status_code=422, detail="approval_execution_token_invalid")
    sqlite = _start(db)
    # A top-up without an approval UUID is an ordinary balance credit (also
    # used by compensating refunds). It has no receipt identity to dedupe and
    # must keep working before the wallet runtime schema is available. SQLite
    # is still serialized by BEGIN IMMEDIATE; the UPDATE itself is atomic on
    # MariaDB. A receipt-correlated top-up must take the full L1/L2 chain.
    if approval_uuid and not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    # L1 - every wallet credit, with or without an approval. A no-op while
    # the Receipt Void schema is not ready (there is no phase row then).
    _lock_wallet_phase(db, sqlite)
    approval_state = None
    effective_mode = "off"
    if approval_uuid:
        from . import receipt_approval_runtime as runtime
        approval_state, effective_mode = _lock_approval_mode(db)   # L2; blocked before lookup

    row = _approval_row(db, approval_uuid) if approval_uuid and receipt_void_schema.is_ready() else None  # L3
    if execution_token is not None and (row is None or row["version"] != execution_token):
        raise HTTPException(status_code=409, detail="approval_superseded")
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
    if strict and execution_token is None:
        # Under 'required' every approval-carrying call presents the current
        # execution token (Receipt Void 5.1); none -> refused before any write.
        raise HTTPException(status_code=422, detail="approval_execution_token_required")

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
          approval_uuid: Optional[str], ensure_can_buy: Callable[[models.User], None],
          execution_token: Optional[int] = None) -> models.User:
    """Retry the complete transaction on known transient lock conflicts."""
    dialect = db.get_bind().dialect.name
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _run_once(db, principal, username, amount, payment_card_id,
                             approval_uuid, ensure_can_buy, execution_token)
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
