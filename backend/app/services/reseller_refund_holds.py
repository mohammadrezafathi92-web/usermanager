"""What must stop an unpaid-service cancellation even when the sale itself
is eligible: evidence that the customer did pay, or other work that owns the
same service.

"Unpaid" here means only what docs/reseller-unpaid-cancellation.md says: a
panel sale charged to the reseller's credit with no payment evidence, which
the reseller confirms the customer never paid. Anything that says money for
THIS service is on its way (a renewal receipt waiting for review, a paid
renewal queued on the service) or that another operation is changing it
refuses the cancellation; nothing here decides who is right. The operator
resolves the other item first.

Checked before a NEW cancellation stops anything. Once an operation exists
it is resumed, not re-judged: its connections may already be stopped or
deleted, and refusing to finish would strand a half-removed service. While
it is pending the customer's other mutations are refused (the fence), and
settlement still re-checks the entitlement and every connection identity.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional

from sqlalchemy import inspect, select

from .. import models

OPEN_APPROVAL_STATES = ("registered", "mutating", "voiding", "cleanup_required")


def _has_table(db, name: str) -> bool:
    return inspect(db.connection()).has_table(name)


def _queued_renewal(row) -> bool:
    # A renewal paid for but not yet active is held on the service (or, for
    # an old account, on the customer); quota/expiry do not change until it
    # activates, so the entitlement check alone cannot see it.
    return bool(row.reserved_created_at is not None or row.reserved_quota_bytes
                or row.reserved_duration_days or row.reserved_package_id is not None)


def _void_held(db, purchase) -> bool:
    from . import receipt_void_schema, reservation_transition
    if not receipt_void_schema.is_ready():
        return False
    return reservation_transition.is_held(db, purchase.history_key)


def _operation_open(db, user_id: int) -> bool:
    if not _has_table(db, "provisioning_operations"):
        return False
    from ..models_provisioning import ProvisioningOperation as Op
    return db.execute(select(Op.id).where(
        (Op.target_user_id == user_id) | (Op.result_user_id == user_id),
        Op.state.notin_(("completed", "compensated"))).limit(1)).first() is not None


def _payment_hold(db, user_id: int) -> bool:
    if not _has_table(db, "payment_reservations"):
        return False
    from ..models_provisioning import PaymentReservation
    return db.execute(select(PaymentReservation.id).where(
        PaymentReservation.user_id == user_id, PaymentReservation.state == "reserved").limit(1)).first() is not None


def _approval_open(db, user_id: int, purchase_id: int) -> bool:
    if not _has_table(db, "receipt_approvals"):
        return False
    from ..models_receipt_void import receipt_approvals as table
    return db.execute(select(table.c.id).where(
        table.c.target_user_id_snapshot == user_id,
        (table.c.target_purchase_id_snapshot == purchase_id) | table.c.target_purchase_id_snapshot.is_(None),
        table.c.state.in_(OPEN_APPROVAL_STATES)).limit(1)).first() is not None


def bot_db_path() -> str:
    return os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")


def _receipt_waiting(user, purchase_id: int) -> Optional[bool]:
    """A renewal receipt for this service still waiting in the bot's queue.

    The queue is the bot's own SQLite file; it is only read here (read-only
    open, never created). Unreadable while it exists -> None (unknown), which
    the caller treats as a hold: an unknown payment is not "unpaid"."""
    path = bot_db_path()
    if not os.path.exists(path):
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return None
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(pending_purchases)")}
        if not columns:
            return False
        if "renew_purchase_id" not in columns:
            # Old queue: no service id; any open renewal for this username counts.
            row = conn.execute("SELECT 1 FROM pending_purchases WHERE kind = 'renew' AND target_username = ? "
                               "AND status IN ('pending', 'processing') LIMIT 1", (user.username,)).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM pending_purchases WHERE kind = 'renew' AND status IN ('pending', 'processing') "
                "AND (renew_purchase_id = ? OR (renew_purchase_id IS NULL AND target_username = ?)) LIMIT 1",
                (purchase_id, user.username)).fetchone()
        return row is not None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def reason(db, purchase: models.Purchase, user: models.User) -> Optional[str]:
    """None when nothing holds this service; otherwise a stable code."""
    if _queued_renewal(purchase) or _queued_renewal(user):
        return "reseller_paid_renewal_queued"
    if _void_held(db, purchase):
        return "reseller_receipt_void_in_progress"
    waiting = _receipt_waiting(user, purchase.id)
    if waiting is None:
        return "reseller_payment_queue_unreadable"
    if waiting or _approval_open(db, user.id, purchase.id):
        return "reseller_payment_pending"
    if _operation_open(db, user.id) or _payment_hold(db, user.id):
        return "reseller_operation_pending"
    return None
