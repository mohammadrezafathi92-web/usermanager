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

from ..models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step


def ppp_blocked(db: Session, connection_id: int) -> bool:
    return db.query(Step.id).join(Operation, Operation.id == Step.operation_id).filter(
        Step.connection_id == connection_id, Step.blocked_at.isnot(None),
        Operation.state == "pending").first() is not None


def blocked_connection_ids(db: Session) -> set[int]:
    return {cid for (cid,) in db.query(Step.connection_id).join(Operation, Operation.id == Step.operation_id).filter(
        Step.blocked_at.isnot(None), Operation.state == "pending")}


def purchase_pending(db: Session, purchase_id) -> bool:
    return purchase_id is not None and db.query(Operation.id).filter_by(
        purchase_id=purchase_id, state="pending").first() is not None
