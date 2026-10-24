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
        "purchase_id": purchase.id, "purchase_key": purchase.history_key,
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
        "operation_id": op.id, "purchase_id": op.retired_purchase_id or op.purchase_id,
        "purchase_key": op.purchase_key, "state": op.state,
        "requested_at": op.requested_at.isoformat() if op.requested_at else None,
        "requested_by": requester.username if requester else None,
        "completed_at": op.completed_at.isoformat() if op.completed_at else None,
        "refund_amount": op.refund_amount,
        "connections_total": len(steps),
        "connections_stopped": sum(1 for s in steps if s.captured_at is not None),
        "connections_removed": sum(1 for s in steps if s.verified_at is not None),
        # Each component as it stands - what is done and what it waits for.
        "components": [_component(db, s) for s in sorted(steps, key=lambda s: s.connection_id)],
    }


def _component(db, step: Step) -> dict:
    connection = db.get(models.Connection, step.connection_id)
    kind = connection.type.value if connection is not None and hasattr(connection.type, "value") else None
    state = ("verified" if step.verified_at else "usage_captured" if step.captured_at
             else "blocked" if step.blocked_at else "pending")
    return {"connection_id": step.connection_id, "type": kind, "state": state,
            "blocked_at": step.blocked_at.isoformat() if step.blocked_at else None,
            "wait_reason": step.wait_reason,
            "wait_since": step.wait_since.isoformat() if step.wait_since else None,
            "open_sessions": db.query(models.RadiusActiveSession).filter_by(connection_id=step.connection_id).count()
            if kind in ("openvpn", "l2tp", "pptp") and not step.verified_at else 0}


def for_user(db, user_id: int) -> list[dict]:
    rows = db.query(Operation).filter_by(user_id=user_id).order_by(Operation.id.desc()).all()
    return [operation(db, op) for op in rows]


def for_purchase(db, purchase: models.Purchase):
    """The pending operation of this live service (a completed one has
    retired its purchase id); never another service's under a reused id."""
    op = db.query(Operation).filter_by(purchase_id=purchase.id).one_or_none()
    if op is None or (op.purchase_key is not None and op.purchase_key != purchase.history_key):
        return None
    return operation(db, op)
