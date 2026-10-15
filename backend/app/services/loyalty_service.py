"""Loyalty counted at PAYMENT time (Receipt Void P9a, sections 5.5, 14.2).

Active only when receipt_approval_runtime_state.loyalty_timing_mode is
'payment_event' (the joint activation, routers: runtime-mode/activate);
until then loyalty works exactly as before and nothing here runs.

Owner decisions (section 23), all four design defaults:
  #15 counted at payment, the same for a new service and a renewal; a
      reserved renewal's later activation counts nothing;
  #16 a sale paid by the reseller's credit and an external integration's
      payment are NOT eligible - only a receipt approval ('card_paid') and a
      captured wallet debit ('wallet_captured') are payment evidence;
  #18 a threshold or reward change opens a forward epoch: past rewards
      stay, no catch-up, progress from each user's anchor;
  #19 the baseline anchor at activation is P - (P mod T).

The truth is the event tables; User.purchase_count and
User.loyalty_rewards_given are caches written in the same transaction:
  active_count = baseline.P + active purchase events (this generation)
  live_rewards = baseline.G + active reward events
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import quota_rewards, receipt_void_schema

PAYMENT_EVENT = "payment_event"
ELIGIBILITY = {"receipt_approval": "card_paid", "wallet_capture": "wallet_captured"}
GB = 1024 ** 3


def _now():
    return dt.datetime.utcnow()


def runtime_row(db, *, lock: bool = False, exclusive: bool = False) -> Optional[dict]:
    if not receipt_void_schema.is_ready():
        return None
    table = rv.receipt_approval_runtime_state
    query = select(table).where(table.c.id == receipt_void_schema.SINGLETON_ID)
    if lock and db.get_bind().dialect.name != "sqlite":
        query = query.with_for_update(read=not exclusive)
    row = db.execute(query).mappings().first()
    return dict(row) if row else None


_PIN = "loyalty_timing_mode_pin"


def pin_mode(db) -> None:
    """A durable T_final reads the loyalty mode ONCE, with a shared lock held
    to its commit, and uses that value throughout: the joint activation
    (exclusive) can then never fall in the middle of a sale - the sale is
    either all legacy (and in the activation's baseline) or all payment-event."""
    row = runtime_row(db, lock=True)
    db.info[_PIN] = (db.get_transaction(), row["loyalty_timing_mode"] if row else None)


def payment_event_mode(db) -> bool:
    pin = db.info.get(_PIN)
    if pin is not None and pin[0] is not None and pin[0] is db.get_transaction():
        return pin[1] == PAYMENT_EVENT
    row = runtime_row(db)
    return bool(row) and row["loyalty_timing_mode"] == PAYMENT_EVENT


def open_epoch(db) -> Optional[dict]:
    table = rv.loyalty_policy_epochs
    row = db.execute(select(table).where(table.c.closed_at.is_(None)).order_by(table.c.id.desc())).mappings().first()
    return dict(row) if row else None


# ------------------------------------------------------------------ counts and caches
def _baseline(db, user_id: int, generation: int) -> tuple[int, int]:
    table = rv.loyalty_user_baselines
    row = db.execute(select(table.c.purchase_count_baseline, table.c.rewards_given_baseline).where(
        table.c.user_id == user_id, table.c.generation == generation)).one_or_none()
    return (int(row[0]), int(row[1])) if row else (0, 0)


def active_count(db, user_id: int, generation: int) -> int:
    events = rv.loyalty_purchase_events
    counted = db.execute(select(func.count()).select_from(events).where(
        events.c.user_id == user_id, events.c.state == "active",
        events.c.completeness_generation == generation)).scalar_one()
    return _baseline(db, user_id, generation)[0] + int(counted)


def live_rewards(db, user_id: int, generation: int) -> int:
    rewards, epochs = rv.loyalty_reward_events, rv.loyalty_policy_epochs
    counted = db.execute(select(func.count()).select_from(rewards.join(epochs, rewards.c.epoch_id == epochs.c.id))
                         .where(rewards.c.user_id == user_id, rewards.c.state == "active",
                                epochs.c.generation == generation)).scalar_one()
    return _baseline(db, user_id, generation)[1] + int(counted)


def write_caches(db, user: models.User, generation: int) -> None:
    user.purchase_count = active_count(db, user.id, generation)
    user.loyalty_rewards_given = live_rewards(db, user.id, generation)


# ------------------------------------------------------------------ the next slot (projection = execution)
@dataclass(frozen=True)
class Slot:
    epoch_id: int
    slot: int
    reearn_generation: int
    required_active_count: int
    credit_amount: int
    quota_bytes: int
    anchor: int


def _anchor(db, user_id: Optional[int], epoch_id: int) -> Optional[int]:
    if user_id is None:
        return None
    table = rv.loyalty_user_epoch_anchors
    return db.execute(select(table.c.starts_after_active_count).where(
        table.c.user_id == user_id, table.c.epoch_id == epoch_id)).scalar_one_or_none()


def next_slot(db, user_id: Optional[int], generation: int, epoch: Optional[dict]) -> Optional[Slot]:
    """The slot THIS eligible purchase would earn, or None (14.2: at most one
    per purchase). user_id None: a customer this sale creates."""
    if epoch is None or int(epoch["threshold"]) <= 0:
        return None
    n_before = active_count(db, user_id, generation) if user_id is not None else 0
    anchor = _anchor(db, user_id, epoch["id"])
    anchor = n_before if anchor is None else anchor
    threshold = int(epoch["threshold"])
    earned = (n_before + 1 - anchor) // threshold
    rewards = rv.loyalty_reward_events
    for slot in range(1, earned + 1):
        rows = [] if user_id is None else db.execute(select(rewards.c.state).where(
            rewards.c.user_id == user_id, rewards.c.epoch_id == epoch["id"], rewards.c.slot == slot)).scalars().all()
        if "active" in rows:
            continue
        return Slot(epoch["id"], slot, 1 + sum(1 for state in rows if state == "voided"),
                    anchor + slot * threshold, int(epoch["credit_amount"]), int(epoch["quota_bytes"]), anchor)
    return None


# ------------------------------------------------------------------ payment evidence (backend-owned)
def record_evidence(db, *, kind: str, sale_entry: models.LedgerEntry, user: models.User, tenant_scope_key: str,
                    approval_uuid: Optional[str] = None, debit_id: Optional[int] = None,
                    wallet_account_id: Optional[int] = None, captured_amount: Optional[int] = None) -> int:
    """One sale_payment_evidence row, in the sale's own transaction; every
    validator failing raises and the sale rolls back (14.2). The approval
    and debit validators are the caller's (T_final checked the approval,
    capture() the debit); here the amounts and the shape."""
    if kind not in ELIGIBILITY:
        raise HTTPException(409, "payment_evidence_kind_invalid")
    amount = int(sale_entry.amount or 0)
    if sale_entry.kind not in ("sale_new", "sale_renew") or amount <= 0 or int(captured_amount or 0) != amount:
        raise HTTPException(409, "payment_evidence_invalid")
    if kind == "wallet_capture":
        debit = db.execute(select(rv.wallet_debit_events).where(
            rv.wallet_debit_events.c.id == debit_id)).mappings().first()
        if debit is None or debit["state"] != "captured" or debit["kind"] != "sale" or (
                debit["ledger_entry_id"] != sale_entry.id or int(debit["amount"]) != amount
                or debit["wallet_account_id"] != wallet_account_id):
            raise HTTPException(409, "payment_evidence_invalid")
    result = db.execute(rv.sale_payment_evidence.insert().values(
        sale_ledger_entry_id=sale_entry.id, tenant_scope_key=tenant_scope_key, evidence_kind=kind,
        approval_uuid=approval_uuid if kind == "receipt_approval" else None,
        wallet_debit_event_id=debit_id if kind == "wallet_capture" else None,
        wallet_account_id=wallet_account_id, user_id_snapshot=user.id, sale_amount=amount,
        captured_amount=amount, created_at=_now()))
    return int(result.inserted_primary_key[0])


# ------------------------------------------------------------------ record_loyalty_purchase
@dataclass(frozen=True)
class Recorded:
    purchase_event_id: int
    reward_event_id: Optional[int]
    slot: Optional[Slot]
    credit: int
    quota_target: object
    quota_granted: int


def record_purchase(db, user: models.User, sale_entry: models.LedgerEntry, evidence_id: int, *,
                    causation: str, approval_uuid: Optional[str] = None, debit_id: Optional[int] = None,
                    on_credit: Optional[Callable] = None) -> Recorded:
    """14.2 record_loyalty_purchase, in the sale's transaction. on_credit(user,
    amount, source_key) applies the reward credit (the caller's wallet
    generation: the lot writer after the cutover)."""
    state = runtime_row(db, lock=True)
    if state is None or state["loyalty_timing_mode"] != PAYMENT_EVENT:
        raise HTTPException(409, "loyalty_not_payment_event")
    generation = int(state["completeness_generation"])
    users = models.User.__table__
    if db.get_bind().dialect.name != "sqlite":
        db.execute(select(users.c.id).where(users.c.id == user.id).with_for_update()).one()
    epoch = open_epoch(db)
    n_before = active_count(db, user.id, generation)
    slot = next_slot(db, user.id, generation, epoch)
    now = _now()
    event = db.execute(rv.loyalty_purchase_events.insert().values(
        user_id=user.id, user_id_snapshot=user.id, payment_evidence_id=evidence_id, ledger_entry_id=sale_entry.id,
        approval_uuid=approval_uuid, eligibility_reason=ELIGIBILITY[causation], completeness_generation=generation,
        state="active", created_at=now)).inserted_primary_key[0]
    if epoch is not None and int(epoch["threshold"]) > 0 and _anchor(db, user.id, epoch["id"]) is None:
        db.execute(rv.loyalty_user_epoch_anchors.insert().values(
            user_id=user.id, user_id_snapshot=user.id, epoch_id=epoch["id"], starts_after_active_count=n_before,
            created_at=now))
    reward_id, credit, target, granted = None, 0, None, 0
    if slot is not None:
        target = quota_rewards.destination(db, user) if slot.quota_bytes > 0 else None
        if slot.credit_amount > 0 or target is not None:
            quota_before = quota_rewards.quota(target) if target is not None else None
            granted = quota_rewards.grant(target, slot.quota_bytes) if target is not None else 0
            if slot.credit_amount <= 0 and granted <= 0:
                slot = None
            else:
                reward_id = _reward_row(db, user, slot, event, causation, approval_uuid, debit_id, sale_entry,
                                        target, quota_before, granted, now)
                if slot.credit_amount > 0 and on_credit is not None:
                    source_id = on_credit(user, slot.credit_amount, reward_source_key(db, user, slot))
                    credit = slot.credit_amount
                    if source_id is not None:
                        db.execute(rv.loyalty_reward_events.update().where(
                            rv.loyalty_reward_events.c.id == reward_id).values(wallet_credit_source_id=source_id))
        else:
            slot = None
    write_caches(db, user, generation)
    return Recorded(int(event), reward_id, slot, credit, target if granted else None, granted)


def _reward_row(db, user, slot, event, causation, approval_uuid, debit_id, sale_entry, target, quota_before,
                granted, now) -> int:
    kind = "Purchase" if isinstance(target, models.Purchase) else ("User" if target is not None else None)
    return int(db.execute(rv.loyalty_reward_events.insert().values(
        user_id=user.id, user_id_snapshot=user.id, epoch_id=slot.epoch_id, slot=slot.slot,
        reearn_generation=slot.reearn_generation, required_active_count=slot.required_active_count,
        triggering_purchase_event_id=event, causation_type=causation,
        causation_approval_uuid=approval_uuid if causation == "receipt_approval" else None,
        causation_wallet_debit_event_id=debit_id if causation == "wallet_capture" else None,
        credit_amount=slot.credit_amount, quota_bytes=granted,
        quota_target_type=kind if granted else None, quota_target_id=target.id if granted else None,
        quota_before=quota_before if granted else None,
        quota_after=(quota_before + granted) if granted else None,
        before_was_unlimited=(quota_before == 0) if granted else None,
        state="active", created_at=now)).inserted_primary_key[0])


def reward_source_key(db, user: models.User, slot: Slot) -> str:
    """loyalty:{lineage}:{epoch}:{slot}:{reearn} (14.2)."""
    lineage = db.execute(select(rv.wallet_accounts.c.lineage_key).where(
        rv.wallet_accounts.c.user_id == user.id)).scalar()
    return f"loyalty:{lineage or 'user-' + str(user.id)}:{slot.epoch_id}:{slot.slot}:{slot.reearn_generation}"


# ------------------------------------------------------------------ policy (epochs)
def _settings(db) -> models.PanelSettings:
    row = db.get(models.PanelSettings, 1)
    if row is None:
        row = models.PanelSettings(id=1)
        db.add(row)
        db.flush()
    return row


def _new_epoch(db, *, threshold: int, credit: int, quota_bytes: int, generation: int, actor_id=None) -> int:
    table = rv.loyalty_policy_epochs
    version = int(db.execute(select(func.coalesce(func.max(table.c.policy_version), 0))).scalar_one()) + 1
    return int(db.execute(table.insert().values(
        threshold=max(0, int(threshold or 0)), credit_amount=max(0, int(credit or 0)),
        quota_bytes=max(0, int(quota_bytes or 0)), generation=generation, policy_version=version,
        started_at=_now(), actor_admin_id=actor_id)).inserted_primary_key[0])


def _policy_values(settings) -> tuple[int, int, int]:
    return (int(settings.loyalty_purchase_threshold or 0), int(settings.loyalty_reward_credit or 0),
            int(round(float(settings.loyalty_reward_gb or 0) * GB)))


def change_policy(db, actor_id: Optional[int], **fields) -> Optional[int]:
    """A threshold / reward change after the activation: the open epoch is
    closed and a new one opens (decision #18), under the exclusive runtime
    lock. The panel settings keep the same values (their display copy)."""
    state = runtime_row(db, lock=True, exclusive=True)
    if state is None or state["loyalty_timing_mode"] != PAYMENT_EVENT:
        raise HTTPException(409, "loyalty_not_payment_event")
    settings = _settings(db)
    before = _policy_values(settings)
    for name, value in fields.items():
        setattr(settings, name, value)
    after = _policy_values(settings)
    if after == before and open_epoch(db) is not None:
        return None
    current = open_epoch(db)
    if current is not None:
        db.execute(rv.loyalty_policy_epochs.update().where(rv.loyalty_policy_epochs.c.id == current["id"]).values(
            closed_at=_now()))
    return _new_epoch(db, threshold=after[0], credit=after[1], quota_bytes=after[2],
                      generation=int(state["completeness_generation"]), actor_id=actor_id)


def capture_baselines(db, generation: int, epoch_id: int, threshold: int) -> int:
    """Activation: every user with a non-zero counter gets its baseline and
    (threshold > 0) its anchor P - (P mod T) (decision #19)."""
    now, count = _now(), 0
    for user_id, purchases, rewards in db.query(models.User.id, models.User.purchase_count,
                                                models.User.loyalty_rewards_given).all():
        p, g = int(purchases or 0), int(rewards or 0)
        if p <= 0 and g <= 0:
            continue
        db.execute(rv.loyalty_user_baselines.insert().values(
            user_id=user_id, generation=generation, purchase_count_baseline=p, rewards_given_baseline=g,
            captured_at=now))
        if threshold > 0:
            db.execute(rv.loyalty_user_epoch_anchors.insert().values(
                user_id=user_id, user_id_snapshot=user_id, epoch_id=epoch_id,
                starts_after_active_count=p - (p % threshold), created_at=now))
        count += 1
    return count
