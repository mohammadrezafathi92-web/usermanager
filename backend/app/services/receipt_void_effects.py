"""Reversal of an approval's effects (Receipt Void 14, 14.1, 14.3) - the
pieces the void steps compose. Each works in the caller's transaction and
never commits.

  mark_effect_reversed   the ONLY writer of an effect's four reversal
                         columns, once (voided_at IS NULL), 14.3;
  reverse_quota          a quota reward taken back from exactly the
                         resource it was written to (14.1): deleted ->
                         resource_deleted; quota changed since -> blocked
                         (cleanup_required, force -> not_applicable);
                         otherwise what the usage allows, the rest a
                         durable shortfall;
  reverse_loyalty        loyalty_reconcile (14.2) for one customer after
                         purchase events were voided: every active reward
                         whose required count is above the new count goes,
                         credit (V on its own source) and quota separately,
                         written in ONE update per reward.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import quota_rewards, wallet_void


class StepBlocked(Exception):
    """The step cannot complete without a decision (cleanup_required)."""

    def __init__(self, code: str, detail: Optional[dict] = None):
        super().__init__(code)
        self.code, self.detail = code, detail or {}


def _now():
    return dt.datetime.utcnow()


def mark_effect_reversed(db, effect_id: int, state: str, value, operation_id: int) -> bool:
    effects = rv.receipt_approval_effects
    return db.execute(effects.update().where(effects.c.id == effect_id, effects.c.voided_at.is_(None)).values(
        reversal_state=state, reversed_value=value, voided_at=_now(), void_operation_id=operation_id)).rowcount == 1


def _quota_columns(resource):
    if isinstance(resource, models.Purchase):
        return "quota_bytes", int(resource.quota_bytes or 0), int(resource.used_bytes or 0)
    return "total_quota_bytes", int(resource.total_quota_bytes or 0), int(resource.used_bytes or 0)


def reverse_quota(db, *, target_type: str, target_id: Optional[int], granted: int, quota_after: int,
                  before_was_unlimited: bool, operation_id: int, user_id_snapshot: int,
                  effect_id: Optional[int] = None, reward_id: Optional[int] = None,
                  forced: bool = False) -> tuple[str, Optional[int]]:
    """(reversal_state, reversed_bytes) per 14.1."""
    model = models.Purchase if target_type == "Purchase" else models.User
    resource = None if target_id is None else db.execute(select(model).where(model.id == target_id)
                                                          .with_for_update()).scalar_one_or_none()
    if resource is None:
        return "resource_deleted", None
    column, quota, used = _quota_columns(resource)
    if quota != int(quota_after):
        if forced:
            return "not_applicable", None
        raise StepBlocked("quota_changed_since_reward", {"type": target_type, "id": target_id})
    if before_was_unlimited:
        setattr(resource, column, 0)
        return "fully_reversed", int(granted)
    reversible = max(min(int(granted), quota - used), 0)
    shortfall = int(granted) - reversible
    setattr(resource, column, quota - reversible)
    _limit_reached(db, resource, quota - reversible, used)
    db.flush()
    if shortfall > 0:
        db.execute(rv.quota_reversal_shortfalls.insert().values(
            receipt_approval_effect_id=effect_id, loyalty_reward_event_id=reward_id, wallet_operation_id=operation_id,
            user_id_snapshot=user_id_snapshot, granted_bytes=int(granted), reversed_bytes=reversible,
            shortfall_bytes=shortfall, created_at=_now()))
        return "partially_reversed", reversible
    return "fully_reversed", reversible


RECONCILE = "receipt_void_reconcile_after_commit"


def _limit_reached(db, resource, quota: int, used: int) -> None:
    """14.1 "reconcile the connections": the quota just went down. At or
    over it, the resource is quota_exceeded in THIS transaction (RADIUS reads
    it live), and its connections are pushed to the nodes right after the
    commit (reconcile_after_commit) - never a node call inside the
    transaction."""
    if quota > 0 and used >= quota and resource.status == models.UserStatus.active:
        resource.status = models.UserStatus.quota_exceeded
    db.info.setdefault(RECONCILE, []).append((type(resource).__name__, resource.id))


def reconcile_after_commit(session_factory, targets) -> None:
    """The existing reconciliation, after the step committed, each target in
    its own short transaction (it may call a node). Best effort: a failure
    leaves the DB status in force and the poller converges."""
    import logging
    from . import user_ops
    for kind, ident in dict.fromkeys(targets or ()):
        try:
            with session_factory() as db:
                target = db.get(models.Purchase if kind == "Purchase" else models.User, ident)
                if target is None:
                    continue
                if kind == "Purchase":
                    user_ops.reconcile_purchase_connections(db, target)
                else:
                    user_ops.reconcile_user_connections(db, target)
                db.commit()
        except Exception:  # noqa: BLE001 - the reversal is committed; the poller converges
            logging.getLogger(__name__).exception("receipt void: reconciliation of %s %s failed", kind, ident)


def effect_quota_facts(effect: dict) -> tuple[str, Optional[int], int, int, bool]:
    snapshot = json.loads(effect["resource_snapshot"] or "{}")
    return (effect["resource_type"], effect["resource_id"], int(effect["delta_value"] or 0),
            int(snapshot.get("quota_after", 0)), bool(snapshot.get("before_was_unlimited", False)))


def forced(key: str, forced_keys) -> bool:
    """A quota reversal an operator forced (13.5, 14.1): its key, or every
    quota key of the operation ('quota:*')."""
    return key in forced_keys or "quota:*" in forced_keys


# ------------------------------------------------------------------ loyalty reconciliation (14.2)
def _active_count(db, user_id: Optional[int], snapshot: int, generation: int) -> int:
    from . import loyalty_service
    events = rv.loyalty_purchase_events
    owner = events.c.user_id == user_id if user_id is not None else (
        events.c.user_id.is_(None)) & (events.c.user_id_snapshot == snapshot)
    counted = db.execute(select(func.count()).select_from(events).where(
        owner, events.c.state == "active", events.c.completeness_generation == generation)).scalar_one()
    return loyalty_service._baseline(db, user_id if user_id is not None else snapshot, generation)[0] + int(counted)


def _owner(table, user_id, snapshot):
    return table.c.user_id == user_id if user_id is not None else (
        table.c.user_id.is_(None)) & (table.c.user_id_snapshot == snapshot)


def lock_user(db, user_id: Optional[int]) -> None:
    """The User row, in the order record_purchase takes it (after the
    loyalty runtime row): purchase recording and reconciliation never both
    count the same customer at once."""
    if user_id is None:
        return
    users = models.User.__table__
    query = select(users.c.id).where(users.c.id == user_id)
    if db.get_bind().dialect.name != "sqlite":          # SQLite: the writer lock of BEGIN IMMEDIATE
        query = query.with_for_update()
    db.execute(query).all()


def loyalty_plan(db, *, user_id: Optional[int], user_id_snapshot: int, void_event_ids) -> dict:
    """14.2 without writing anything: the active count once the given purchase
    events are voided, and every active reward that count no longer backs
    (any causation - possibly a later, healthy approval's). The preview and
    reverse_loyalty use exactly this."""
    from . import loyalty_service
    events, rewards = rv.loyalty_purchase_events, rv.loyalty_reward_events
    state = loyalty_service.runtime_row(db)
    generation = int(state["completeness_generation"]) if state else 0
    ids = [int(i) for i in void_event_ids]
    leaving = 0 if not ids else int(db.execute(select(func.count()).select_from(events).where(
        events.c.id.in_(ids), events.c.state == "active", events.c.completeness_generation == generation)).scalar_one())
    count = _active_count(db, user_id, user_id_snapshot, generation) - leaving
    invalid = [dict(r) for r in db.execute(select(rewards).where(
        _owner(rewards, user_id, user_id_snapshot), rewards.c.state == "active",
        rewards.c.required_active_count > count).order_by(
            rewards.c.required_active_count.desc(), rewards.c.id.desc())).mappings()]
    return {"active_count_after": count, "rewards": invalid, "generation": generation}


def reverse_loyalty(db, *, user_id: Optional[int], user_id_snapshot: int, purchase_event_ids: list,
                    operation_id: int, causing_approval_uuid: Optional[str], sale_refund: bool,
                    customer_identity_id: Optional[int], forced_keys: frozenset = frozenset()) -> dict:
    """loyalty_reconcile: void the given purchase events, then every active
    reward of this customer above the new count. user_id None: the customer
    was deleted by this same void - its events and rewards are found by
    snapshot and no cache is written. Locks: the loyalty runtime row, then
    the User row (the order of record_purchase), before anything is counted."""
    from . import loyalty_service
    events, rewards = rv.loyalty_purchase_events, rv.loyalty_reward_events
    state = loyalty_service.runtime_row(db, lock=True)
    generation = int(state["completeness_generation"]) if state else 0
    lock_user(db, user_id)
    now = _now()
    for event_id in purchase_event_ids:
        db.execute(events.update().where(events.c.id == event_id, events.c.state == "active").values(
            state="voided", voided_at=now, void_operation_id=operation_id))
    count = _active_count(db, user_id, user_id_snapshot, generation)
    owner = _owner(rewards, user_id, user_id_snapshot)
    voided = []
    for reward in db.execute(select(rewards).where(owner, rewards.c.state == "active").order_by(
            rewards.c.required_active_count.desc(), rewards.c.id.desc())).mappings().all():
        if int(reward["required_active_count"]) <= count:
            continue
        reason = ("receipt_void" if causing_approval_uuid and reward["causation_approval_uuid"] == causing_approval_uuid
                  else "sale_refund" if sale_refund else "loyalty_reconciliation")
        credit_state = credit_value = quota_state = quota_value = None
        if int(reward["credit_amount"]) > 0:
            if reward["wallet_credit_source_id"] is None:
                credit_state, credit_value = "unrecoverable", 0
            else:
                result = wallet_void.void_source(db, int(reward["wallet_credit_source_id"]), operation_id,
                                                 customer_identity_id)
                credit_state, credit_value = result.reversal_state, result.reversed_value
        if int(reward["quota_bytes"]) > 0:
            key = f"quota:loyalty:{reward['epoch_id']}:{reward['slot']}"
            quota_state, quota_value = reverse_quota(
                db, target_type=reward["quota_target_type"] or "User", target_id=reward["quota_target_id"],
                granted=int(reward["quota_bytes"]), quota_after=int(reward["quota_after"] or 0),
                before_was_unlimited=bool(reward["before_was_unlimited"]), operation_id=operation_id,
                user_id_snapshot=user_id_snapshot, reward_id=reward["id"], forced=forced(key, forced_keys))
        if quota_state not in ("fully_reversed", "partially_reversed"):
            quota_value = None
        changed = db.execute(rewards.update().where(rewards.c.id == reward["id"], rewards.c.state == "active").values(
            state="voided", void_reason=reason, voided_at=now, void_operation_id=operation_id,
            credit_reversal_state=credit_state, credit_reversed_amount=credit_value,
            quota_reversal_state=quota_state, quota_reversed_bytes=quota_value)).rowcount
        if changed != 1:
            raise HTTPException(409, "loyalty_reward_already_reconciled")
        if reward["causation_type"] == "receipt_approval":
            _mark_loyalty_effects(db, reward, credit_state, credit_value, quota_state, quota_value, operation_id)
        voided.append(int(reward["id"]))
    if user_id is not None:
        user = db.get(models.User, user_id)
        if user is not None:
            loyalty_service.write_caches(db, user, generation)
    return {"voided_purchase_events": list(purchase_event_ids), "voided_rewards": voided, "active_count": count}


def _mark_loyalty_effects(db, reward, credit_state, credit_value, quota_state, quota_value, operation_id):
    effects = rv.receipt_approval_effects
    slot = f"{reward['epoch_id']}:{reward['slot']}"
    for key, state, value in ((f"credit:loyalty:{slot}", credit_state, credit_value),
                              (f"quota:loyalty:{slot}", quota_state, quota_value)):
        if state is None:
            continue
        effect_id = db.execute(select(effects.c.id).where(
            effects.c.approval_uuid == reward["causation_approval_uuid"], effects.c.effect_key == key)).scalar()
        if effect_id is not None:
            mark_effect_reversed(db, effect_id, state, value if state not in ("resource_deleted", "not_applicable")
                                 else None, operation_id)
