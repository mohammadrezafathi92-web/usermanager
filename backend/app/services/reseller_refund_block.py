"""Which connections an unpaid-service cancellation holds right now.

A PPP (RADIUS) connection has no object on the router to switch off: its
logins are refused by radius_server while its step is blocked, and its open
sessions must end with an Accounting Stop before its usage is final. Every
check here is independent of RESELLER_CANCELLATION_ENABLED: switching the
feature off never lifts the hold of an operation that is still pending.
Only the settlement (which deletes the service) ends it.
"""
from __future__ import annotations

from sqlalchemy.orm import Session
from sqlalchemy import or_

from ..models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step


def session_key(value) -> str | None:
    """One form for a RouterOS /ppp/active session-id ('0x81C00009') and the
    RADIUS Acct-Session-Id of that session ('81c00009'). To be confirmed on
    a staging RouterOS; a mismatch only ever leaves a cancellation pending."""
    if value is None:
        return None
    text = str(value).strip().lower()
    text = text[2:] if text.startswith("0x") else text
    return text or None


def ppp_blocked(db: Session, connection_id: int) -> bool:
    return db.query(Step.id).join(Operation, Operation.id == Step.operation_id).filter(
        Step.connection_id == connection_id, Step.blocked_at.isnot(None),
        Operation.state == "pending").first() is not None


def note_unverified(db: Session, connection_id: int) -> None:
    """Accounting this connection's grants cannot vouch for arrived while
    it is held (radius_server): recorded on its pending step (same commit)."""
    from datetime import datetime
    pending = db.query(Operation.id).filter(Operation.state == "pending")
    db.query(Step).filter(Step.connection_id == connection_id, Step.blocked_at.isnot(None),
                          Step.ppp_unverified_at.is_(None), Step.operation_id.in_(pending)).update(
        {"ppp_unverified_at": datetime.utcnow()}, synchronize_session=False)


def blocked_connection_ids(db: Session) -> set[int]:
    return {cid for (cid,) in db.query(Step.connection_id).join(Operation, Operation.id == Step.operation_id).filter(
        Step.blocked_at.isnot(None), Operation.state == "pending")}


def holding_operations(db: Session):
    """Only an operation that may already have mutated owns its service.

    Step evidence is also checked for operations created by older versions,
    before mutation_started_at existed. Never release a real pending stop
    merely because its feature flag was switched off.
    """
    changed_steps = db.query(Step.operation_id).filter(or_(
        Step.blocked_at.isnot(None), Step.captured_at.isnot(None), Step.verified_at.isnot(None)))
    return db.query(Operation).filter(Operation.state == "pending", or_(
        Operation.mutation_started_at.isnot(None), Operation.id.in_(changed_steps)))


def purchase_pending(db: Session, purchase_id) -> bool:
    return purchase_id is not None and holding_operations(db).filter(
        Operation.purchase_id == purchase_id).first() is not None


def user_pending(db: Session, user_id) -> bool:
    return user_id is not None and holding_operations(db).filter(Operation.user_id == user_id).first() is not None
