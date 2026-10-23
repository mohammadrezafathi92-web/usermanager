"""Repairing ONE Purchase's concurrent-session cap, previewed then executed.

Two fields: max_concurrent_sessions (the cap in force) and
reserved_max_concurrent_sessions (the cap a queued renewal will put in
force when it activates - only on a Purchase that has a queued renewal
with a package; the cap in force is left as it is).

The preview shows the exact state the change would apply to - the
Purchase, its customer and the User's cap, every connection in the
Purchase's scope and the live sessions on them - and a token binding all
of it. Execute re-reads the same state inside its own locked transaction
and writes only if the token still matches (compare-and-set on the
Purchase, the User and the current value, and on the scope's
connections), then records an audit row in the same commit. Nothing is
ever written in bulk: one Purchase per call.

Under session_cap_mode 'legacy'/'shadow' a Purchase's cap decides nothing
(the legacy rule never reads it); it governs only once 'enforced'. There it
is the only cap of the Purchase's connections: neither the User's cap nor a
connection's own (a PPP credential's, often 1) is applied on top of it
(session_cap_rule.session_scope), so raising it is never held back by them.

Refused while other work is changing the same service (a receipt-void hold,
an open receipt approval, an unfinished provisioning operation or reserved
payment, a pending unpaid-service cancellation): that work could write the
cap from what it read before this change. The operator lets it finish and
previews again. A queued renewal is not such work - its cap is the separate
reserved field, never touched by a change of the cap in force.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select, text

from .. import models
from . import session_cap_rule

# The cap a single form may set: whole numbers only, 1 or more (0/NULL mean
# "no cap" in session_cap_rule and are never set from here). The upper bound
# only stops a typo (10000 for 10); real caps are single digits.
MAX_CAP = 1000
FIELDS = ("max_concurrent_sessions", "reserved_max_concurrent_sessions")


def _package_cap(db, package_id):
    """The linked package's cap now - shown so the operator can check the
    source, and bound in the token so a package edited after the preview
    makes the execute refuse."""
    if package_id is None:
        return None
    package = db.get(models.Package, package_id)
    return package.max_concurrent_sessions if package is not None else None


def _busy(db, purchase, user) -> Optional[str]:
    from ..models_reseller_refund import ResellerRefundOperation
    from . import reseller_refund_holds
    held = reseller_refund_holds.in_flight(db, purchase, user)
    if held:
        return f"purchase_cap_{held}_in_progress"
    if db.query(ResellerRefundOperation.id).filter(ResellerRefundOperation.user_id == user.id,
                                                  ResellerRefundOperation.state == "pending").first():
        return "purchase_cap_cancellation_pending"
    return None


def _state(db, purchase_id: int, field: str, *, lock: bool = False) -> dict:
    query = select(models.Purchase).where(models.Purchase.id == purchase_id)
    purchase = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if purchase is None:
        raise HTTPException(404, "purchase_not_found")
    # Old installations may not have a history key yet. Their creation
    # timestamp is the fallback identity; never bind only a recyclable id.
    if purchase.history_key is None and purchase.created_at is None:
        raise HTTPException(409, "purchase_cap_identity_unavailable")
    user = db.get(models.User, purchase.user_id)
    connections = sorted(db.query(models.Connection).filter(models.Connection.user_id == user.id),
                         key=lambda c: c.id)
    scope = [c for c in connections if c.purchase_id == purchase.id]
    legacy = [c for c in connections if c.purchase_id is None and c.enabled]
    counts = session_cap_rule.ppp_counts(db, [user.id])
    return dict(
        purchase_id=purchase.id, user_id=user.id, username=user.username,
        purchase_history_key=purchase.history_key, purchase_created_at=purchase.created_at,
        reservation_activation_seq=int(purchase.reservation_activation_seq or 0),
        renewal_event_seq=int(purchase.renewal_event_seq or 0),
        purchase_status=purchase.status.value if purchase.status else None,
        field=field, current_value=getattr(purchase, field),
        cap_in_force=purchase.max_concurrent_sessions,
        user_cap=user.max_concurrent_sessions,
        package_id=purchase.package_id,
        package_cap=_package_cap(db, purchase.package_id),
        reserved_package_id=purchase.reserved_package_id,
        reserved_package_cap=_package_cap(db, purchase.reserved_package_id),
        scope=[dict(connection_id=c.id, type=c.type.value, enabled=bool(c.enabled),
                    connection_cap=c.max_concurrent_sessions,
                    live=counts.get(c.id, 0) if c.type in session_cap_rule.PPP_TYPES else int(bool(c.online)))
               for c in scope],
        other_enabled_legacy_connections=[c.id for c in legacy],
        busy=_busy(db, purchase, user),
    )


def _token(state: dict, new_value: int) -> str:
    bound = json.loads(json.dumps(dict(state, new_value=new_value), default=str))
    bound.pop("busy", None)    # checked on its own, at preview and again under the execute lock
    for row in bound["scope"]:
        row.pop("live", None)  # live sessions inform the preview; they do not bind it
    return hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode()).hexdigest()


def _valid(new_value, field: str) -> int:
    if field not in FIELDS:
        raise HTTPException(422, "purchase_cap_field_invalid")
    if not isinstance(new_value, int) or isinstance(new_value, bool) or not 1 <= new_value <= MAX_CAP:
        raise HTTPException(422, "purchase_cap_value_invalid")
    return new_value


def _reservation_required(state: dict) -> None:
    if state["field"] == "reserved_max_concurrent_sessions" and state["reserved_package_id"] is None:
        raise HTTPException(409, "purchase_cap_no_queued_renewal")


def preview(db, purchase_id: int, new_value: int, field: str = "max_concurrent_sessions") -> dict:
    from . import session_cap_mode
    new_value = _valid(new_value, field)
    state = _state(db, purchase_id, field)
    _reservation_required(state)
    live = sum(row["live"] for row in state["scope"] if row["enabled"])
    ppp = {t.value for t in session_cap_rule.PPP_TYPES}
    live_ppp = sum(row["live"] for row in state["scope"] if row["enabled"] and row["type"] in ppp)
    token = _token(state, new_value)
    db.rollback()
    # The EFFECTIVE mode, read exactly as RADIUS and the poller read it
    # (session_cap_mode._read): no state row and never changed = the
    # default; a lost or invalid row = no decision at all ('unavailable').
    try:
        mode = session_cap_mode._read(db.get_bind())
    except session_cap_mode.ModeUnavailable:
        mode = "unavailable"
    # What the change does now, for the confirmation (the UI words it):
    # in 'enforced' a cap below the live count kicks WireGuard/Xray on the
    # next poll (newest first) and refuses new PPP logins; open PPP sessions
    # are not cut, they end on their own.
    excess = max(live - new_value, 0) if field == "max_concurrent_sessions" else 0
    enforced = (f"at most {new_value} live session(s) across this Purchase's connections combined; {live} live now"
                if field == "max_concurrent_sessions" else
                f"none now; {new_value} becomes this Purchase's cap when its queued renewal activates")
    return dict(state, new_value=new_value, live_sessions_in_scope=live, preview_token=token,
                session_cap_mode=mode, live_ppp_sessions=live_ppp, live_other_sessions=live - live_ppp,
                excess_now=excess, in_force_now=mode == "enforced" and field == "max_concurrent_sessions",
                effect=dict(legacy_and_shadow="none: the legacy rule never reads a Purchase's cap", enforced=enforced))


def execute(session_factory, admin_id: int, *, purchase_id: int, expected_user_id: int,
            expected_current: Optional[int], new_value: int, preview_token: str, reason: str,
            field: str = "max_concurrent_sessions") -> dict:
    new_value = _valid(new_value, field)
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(422, "purchase_cap_reason_required")
    with session_factory() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        state = _state(db, purchase_id, field, lock=True)
        _reservation_required(state)
        if state["busy"]:
            db.rollback()
            raise HTTPException(409, state["busy"])
        if state["user_id"] != expected_user_id or state["current_value"] != expected_current:
            db.rollback()
            raise HTTPException(409, "purchase_cap_target_changed")
        if _token(state, new_value) != preview_token:
            db.rollback()
            raise HTTPException(409, "purchase_cap_preview_stale")
        column = getattr(models.Purchase, field)
        result = db.execute(models.Purchase.__table__.update().where(
            models.Purchase.id == purchase_id, models.Purchase.user_id == expected_user_id,
            models.Purchase.history_key.is_(None) if state["purchase_history_key"] is None
            else models.Purchase.history_key == state["purchase_history_key"],
            models.Purchase.created_at.is_(None) if state["purchase_created_at"] is None
            else models.Purchase.created_at == state["purchase_created_at"],
            func.coalesce(models.Purchase.reservation_activation_seq, 0) == state["reservation_activation_seq"],
            func.coalesce(models.Purchase.renewal_event_seq, 0) == state["renewal_event_seq"],
            column.is_(None) if expected_current is None else column == expected_current,
        ).values(**{field: new_value}))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "purchase_cap_target_changed")
        audit = models.PurchaseCapRepair(
            purchase_id=purchase_id, user_id=expected_user_id, field=field, before_value=expected_current,
            after_value=new_value, user_cap_at_repair=state["user_cap"],
            scope_connection_ids=json.dumps([row["connection_id"] for row in state["scope"]]),
            preview_token=preview_token, reason=reason[:2000], admin_id=admin_id)
        db.add(audit)
        db.commit()
        return dict(purchase_id=purchase_id, field=field, before=expected_current, after=new_value, repair_id=audit.id)
