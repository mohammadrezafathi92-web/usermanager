"""Repairing ONE Purchase's concurrent-session cap, previewed then executed.

The preview shows the exact state the change would apply to - the
Purchase, its customer and the User's cap, every connection in the
Purchase's scope and the live sessions on them - and a token binding all
of it. Execute re-reads the same state inside its own locked transaction
and writes only if the token still matches (compare-and-set on the
Purchase, the User and the current value, and on the scope's
connections), then records an audit row in the same commit. Nothing is
ever written in bulk: one Purchase per call.

Under session_cap_mode 'legacy'/'shadow' a Purchase's cap decides nothing
(the legacy rule never reads it); it governs only once 'enforced'.
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text

from .. import models
from . import session_cap_rule

MAX_CAP = 1000


def _state(db, purchase_id: int, *, lock: bool = False) -> dict:
    query = select(models.Purchase).where(models.Purchase.id == purchase_id)
    purchase = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if purchase is None:
        raise HTTPException(404, "purchase_not_found")
    user = db.get(models.User, purchase.user_id)
    connections = sorted(db.query(models.Connection).filter(models.Connection.user_id == user.id),
                         key=lambda c: c.id)
    scope = [c for c in connections if c.purchase_id == purchase.id]
    legacy = [c for c in connections if c.purchase_id is None and c.enabled]
    counts = session_cap_rule.ppp_counts(db, [user.id])
    return dict(
        purchase_id=purchase.id, user_id=user.id, username=user.username,
        purchase_status=purchase.status.value if purchase.status else None,
        current_value=purchase.max_concurrent_sessions,
        user_cap=user.max_concurrent_sessions,
        package_id=purchase.package_id,
        scope=[dict(connection_id=c.id, type=c.type.value, enabled=bool(c.enabled),
                    connection_cap=c.max_concurrent_sessions,
                    live=counts.get(c.id, 0) if c.type in session_cap_rule.PPP_TYPES else int(bool(c.online)))
               for c in scope],
        other_enabled_legacy_connections=[c.id for c in legacy],
    )


def _token(state: dict, new_value: int) -> str:
    bound = json.loads(json.dumps(dict(state, new_value=new_value), default=str))
    for row in bound["scope"]:
        row.pop("live", None)  # live sessions inform the preview; they do not bind it
    return hashlib.sha256(json.dumps(bound, sort_keys=True, default=str).encode()).hexdigest()


def _valid(new_value) -> int:
    if not isinstance(new_value, int) or isinstance(new_value, bool) or not 1 <= new_value <= MAX_CAP:
        raise HTTPException(422, "purchase_cap_value_invalid")
    return new_value


def preview(db, purchase_id: int, new_value: int) -> dict:
    new_value = _valid(new_value)
    state = _state(db, purchase_id)
    live = sum(row["live"] for row in state["scope"] if row["enabled"])
    token = _token(state, new_value)
    db.rollback()
    return dict(state, new_value=new_value, live_sessions_in_scope=live, preview_token=token,
                effect=dict(
                    legacy_and_shadow="none: the legacy rule never reads a Purchase's cap",
                    enforced=f"at most {new_value} live session(s) across this Purchase's connections combined; "
                             f"{live} live now"))


def execute(session_factory, admin_id: int, *, purchase_id: int, expected_user_id: int,
            expected_current: Optional[int], new_value: int, preview_token: str, reason: str) -> dict:
    new_value = _valid(new_value)
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(422, "purchase_cap_reason_required")
    with session_factory() as db:
        if db.get_bind().dialect.name == "sqlite":
            db.execute(text("BEGIN IMMEDIATE"))
        state = _state(db, purchase_id, lock=True)
        if state["user_id"] != expected_user_id or state["current_value"] != expected_current:
            db.rollback()
            raise HTTPException(409, "purchase_cap_target_changed")
        if _token(state, new_value) != preview_token:
            db.rollback()
            raise HTTPException(409, "purchase_cap_preview_stale")
        column = models.Purchase.max_concurrent_sessions
        result = db.execute(models.Purchase.__table__.update().where(
            models.Purchase.id == purchase_id, models.Purchase.user_id == expected_user_id,
            column.is_(None) if expected_current is None else column == expected_current,
        ).values(max_concurrent_sessions=new_value))
        if result.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "purchase_cap_target_changed")
        audit = models.PurchaseCapRepair(
            purchase_id=purchase_id, user_id=expected_user_id, before_value=expected_current,
            after_value=new_value, user_cap_at_repair=state["user_cap"],
            scope_connection_ids=json.dumps([row["connection_id"] for row in state["scope"]]),
            preview_token=preview_token, reason=reason[:2000], admin_id=admin_id)
        db.add(audit)
        db.commit()
        return dict(purchase_id=purchase_id, before=expected_current, after=new_value, repair_id=audit.id)
