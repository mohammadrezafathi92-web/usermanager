"""Read-only views for the unpaid-cancellation UI: what exactly would be
cancelled, and where an existing operation stands. Never moves money and
never calls a node."""
from __future__ import annotations

from .. import models
from ..models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from ..models_reseller_refund import ResellerSaleBasis as Basis


def target(db, purchase: models.Purchase) -> dict:
    """The one service the cancellation touches - and what it leaves alone."""
    basis = db.query(Basis).filter_by(purchase_id=purchase.id).one_or_none()
    charged = db.get(models.AdminUser, basis.charged_admin_id) if basis else None
    user = purchase.user
    return {
        "purchase_id": purchase.id,
        "package_name": purchase.package_name_snapshot or (purchase.package.name if purchase.package else None),
        "comment": purchase.comment,
        "created_at": purchase.created_at.isoformat() if purchase.created_at else None,
        "expire_at": purchase.expire_at.isoformat() if purchase.expire_at else None,
        "quota_bytes": int(purchase.quota_bytes or 0),
        "used_bytes": int(purchase.used_bytes or 0),
        "connections": [{"id": c.id, "type": c.type.value if hasattr(c.type, "value") else str(c.type),
                         "node": c.node.name if c.node else None, "name": c.wg_peer_name}
                        for c in purchase.connections],
        "other_services": sum(1 for p in user.purchases if p.id != purchase.id),
        "charged_admin": charged.username if charged else None,
    }


def operation(db, op: Operation) -> dict:
    steps = db.query(Step).filter_by(operation_id=op.id).all()
    requester = db.get(models.AdminUser, op.requested_by)
    return {
        "operation_id": op.id, "purchase_id": op.purchase_id, "state": op.state,
        "requested_at": op.requested_at.isoformat() if op.requested_at else None,
        "requested_by": requester.username if requester else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
        "refund_amount": op.refund_amount,
        "connections_total": len(steps),
        "connections_stopped": sum(1 for s in steps if s.captured_at is not None),
        "connections_removed": sum(1 for s in steps if s.verified_at is not None),
    }


def for_user(db, user_id: int) -> list[dict]:
    rows = db.query(Operation).filter_by(user_id=user_id).order_by(Operation.id.desc()).all()
    return [operation(db, op) for op in rows]


def for_purchase(db, purchase_id: int):
    op = db.query(Operation).filter_by(purchase_id=purchase_id).one_or_none()
    return operation(db, op) if op is not None else None
