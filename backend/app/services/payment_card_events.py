"""Causal event log of a payment-card pool (Receipt Void design, section 15;
rollout phase P2).

A pool is either 'legacy' (nothing is logged - exactly how the panel always
worked) or 'event_logged': a baseline of every card's counter was captured
once, and from then on each confirmed payment writes one event row in the
SAME transaction as the live counter/pointer write. With that, voiding one
fake payment later can take back exactly that payment's amount (and the
counter reset it caused) and nothing else - see accumulator().

Everything here is fenced by a locking read of the pool's state row, so a
payment is either completely inside the baseline or completely an event.

Nothing in this module commits, and nothing in it may make a payment
approval fail: when the event cannot be written the pool is put back to
'legacy' (its events are then ignored until a new baseline is captured).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from . import receipt_void_schema

log = logging.getLogger(__name__)

LEGACY = "legacy"
EVENT_LOGGED = "event_logged"
# Reserved key inside payment_card_pool_baselines.accumulated_by_card: the
# highest event id of the pool when the baseline was taken. Events up to it
# belong to an earlier logging period and are ignored.
AFTER_EVENT_KEY = "_after_event_id"


def pool_key(owner_admin_id: Optional[int]) -> str:
    return "global" if owner_admin_id is None else f"admin:{owner_admin_id}"


def enabled() -> bool:
    return receipt_void_schema.is_ready()


def _tables():
    from .. import models_receipt_void as rv
    return rv.payment_card_pool_states, rv.payment_card_pool_baselines, rv.payment_card_pool_events


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


def lock_pool(db: Session, owner_admin_id: Optional[int], *, create_as: Optional[str] = LEGACY) -> Optional[str]:
    """Locking read of the pool's state row; returns its logging phase.
    A pool seen for the first time gets a row (create_as), so that the next
    writer has something to lock. None if there is no row and create_as is
    None. Raises on a database error - callers on the payment path go
    through phase_for_payment instead.

    (On SQLite a savepoint opened before the transaction's first write
    commits on release, so the freshly created row may outlive a rollback.
    It is only ever created as 'legacy', which means the same as no row.)"""
    states, _baselines, _events = _tables()
    key = pool_key(owner_admin_id)
    query = select(states.c.logging_phase).where(states.c.pool_key == key).with_for_update()
    phase = db.execute(query).scalar()
    if phase is not None or create_as is None:
        return phase
    try:
        with db.begin_nested():
            db.execute(states.insert().values(pool_key=key, logging_phase=create_as, epoch=0,
                                              updated_at=_now(), version=0))
    except IntegrityError:
        pass                        # another writer created it first
    return db.execute(query).scalar()


def phase_for_payment(db: Session, owner_admin_id: Optional[int]) -> str:
    """lock_pool for the payment path: any problem means 'legacy'."""
    if not enabled():
        return LEGACY
    try:
        with db.begin_nested():
            return lock_pool(db, owner_admin_id) or LEGACY
    except Exception:
        log.exception("card pool %s: state row could not be locked; payment recorded without an event",
                      pool_key(owner_admin_id))
        return LEGACY


def _set_phase(db: Session, key: str, phase: str) -> None:
    states, _baselines, _events = _tables()
    db.execute(states.update().where(states.c.pool_key == key).values(
        logging_phase=phase, epoch=states.c.epoch + 1, version=states.c.version + 1, updated_at=_now()))


def card_label(card: models.PaymentCard) -> str:
    """Last four digits + holder name only - never the full card number."""
    digits = "".join(ch for ch in (card.card_number or "") if ch.isdigit())
    return f"{digits[-4:]} {card.card_holder or ''}".strip()[:32]


def record_payment(db: Session, card: models.PaymentCard, *, amount: int, accumulated_before: int,
                   active_card_id_before: Optional[int], card_order: list[int], reset_after: bool,
                   rotated_to_card_id: Optional[int], mode: str, threshold: Optional[int],
                   approval_uuid: Optional[str] = None) -> bool:
    """Writes the event row of one payment, with the values the live logic
    just used. On any failure the pool goes back to 'legacy' and the payment
    itself is unaffected. Returns whether the event was written."""
    _states, _baselines, events = _tables()
    key = pool_key(card.owner_admin_id)
    try:
        if reset_after:
            expected = None
            if len(card_order) > 1 and card.id in card_order:
                expected = card_order[(card_order.index(card.id) + 1) % len(card_order)]
            if rotated_to_card_id != expected:
                raise ValueError(f"rotated_to {rotated_to_card_id!r} is not the next card {expected!r}")
        with db.begin_nested():
            db.execute(events.insert().values(
                pool_key=key,
                event_kind="payment_recorded" if approval_uuid else "payment_recorded_uncorrelated",
                card_id=card.id, card_id_snapshot=card.id, card_label_snapshot=card_label(card),
                approval_uuid=approval_uuid, amount=amount, accumulated_before=accumulated_before,
                active_card_id_before=active_card_id_before, card_order_snapshot=json.dumps(card_order),
                reset_after=reset_after, rotated_to_card_id_snapshot=rotated_to_card_id,
                mode_snapshot=(mode or "manual")[:16], threshold_snapshot=threshold, created_at=_now()))
        return True
    except Exception:
        log.exception("card pool %s: payment event could not be written; pool set back to 'legacy'", key)
        try:
            with db.begin_nested():
                _set_phase(db, key, LEGACY)
        except Exception:
            log.exception("card pool %s: could not be set back to 'legacy' either", key)
        return False


def capture_baseline(db: Session, owner_admin_id: Optional[int]) -> dict:
    """legacy -> event_logged for one pool, in the caller's transaction:
    every card's live counter becomes the baseline. A pool that is already
    event_logged is left alone."""
    receipt_void_schema.assert_ready()
    _states, baselines, events = _tables()
    key = pool_key(owner_admin_id)
    if lock_pool(db, owner_admin_id) == EVENT_LOGGED:
        return {"pool_key": key, "changed": False, "phase": EVENT_LOGGED}
    cards = (db.query(models.PaymentCard).filter(models.PaymentCard.owner_admin_id == owner_admin_id)
             .populate_existing().with_for_update().all())
    payload: dict = {str(card.id): int(card.accumulated_amount or 0) for card in cards}
    payload[AFTER_EVENT_KEY] = int(db.execute(
        select(func.coalesce(func.max(events.c.id), 0)).where(events.c.pool_key == key)).scalar() or 0)
    values = {"accumulated_by_card": json.dumps(payload, sort_keys=True), "captured_at": _now()}
    if db.execute(select(baselines.c.pool_key).where(baselines.c.pool_key == key)).first():
        db.execute(baselines.update().where(baselines.c.pool_key == key).values(**values))
    else:
        db.execute(baselines.insert().values(pool_key=key, **values))
    _set_phase(db, key, EVENT_LOGGED)
    return {"pool_key": key, "changed": True, "phase": EVENT_LOGGED, "cards": len(cards)}


def revert_to_legacy(db: Session, owner_admin_id: Optional[int]) -> dict:
    """event_logged -> legacy (the P2 rollback). Events stay and are ignored."""
    receipt_void_schema.assert_ready()
    key = pool_key(owner_admin_id)
    if lock_pool(db, owner_admin_id) == LEGACY:
        return {"pool_key": key, "changed": False, "phase": LEGACY}
    _set_phase(db, key, LEGACY)
    return {"pool_key": key, "changed": True, "phase": LEGACY}


def start_new_pool(db: Session, owner_admin_id: Optional[int]) -> None:
    """Called when the FIRST card of a pool is created: a pool the panel has
    never seen starts directly as event_logged with an empty baseline. A
    pool that already has a state row keeps its phase. Never raises."""
    if not enabled():
        return
    try:
        with db.begin_nested():
            if lock_pool(db, owner_admin_id, create_as=None) is None:
                capture_baseline(db, owner_admin_id)      # the card insert was flushed: a real transaction is open
    except Exception:
        log.exception("card pool %s: could not start its event log", pool_key(owner_admin_id))


def detach_card(db: Session, owner_admin_id: Optional[int], card_id: int) -> None:
    """Before a card row is deleted: its events keep card_id_snapshot and
    lose the live reference (what ON DELETE SET NULL does on MariaDB; SQLite
    does not enforce it), and its baseline entry is dropped. SQLite hands a
    deleted card's id to the next card, so without this a new card would
    inherit the old one's history. Never raises."""
    if not enabled():
        return
    _states, baselines, events = _tables()
    key = pool_key(owner_admin_id)
    try:
        with db.begin_nested():
            lock_pool(db, owner_admin_id, create_as=None)
            db.execute(events.update().where(events.c.card_id == card_id).values(card_id=None))
            raw = db.execute(select(baselines.c.accumulated_by_card).where(baselines.c.pool_key == key)).scalar()
            payload = json.loads(raw) if raw else {}
            if payload.pop(str(card_id), None) is not None:
                db.execute(baselines.update().where(baselines.c.pool_key == key).values(
                    accumulated_by_card=json.dumps(payload, sort_keys=True)))
    except Exception:
        log.exception("card %s: events could not be detached before delete", card_id)


def accumulator(db: Session, owner_admin_id: Optional[int], card_id: int) -> int:
    """Design 15.3: the counter of one LIVE card, from the baseline and that
    card's own non-voided events only (events of a deleted card have lost
    their card_id - see detach_card - and never count for a later card)."""
    _states, baselines, events = _tables()
    key = pool_key(owner_admin_id)
    raw = db.execute(select(baselines.c.accumulated_by_card).where(baselines.c.pool_key == key)).scalar()
    baseline = json.loads(raw) if raw else {}
    total = int(baseline.get(str(card_id), 0))
    rows = db.execute(
        select(events.c.amount, events.c.reset_after)
        .where(events.c.pool_key == key, events.c.card_id_snapshot == card_id, events.c.card_id == card_id,
               events.c.id > int(baseline.get(AFTER_EVENT_KEY, 0)), events.c.voided_at.is_(None))
        .order_by(events.c.id))
    for amount, reset_after in rows:
        total = 0 if reset_after else total + int(amount)
    return total


def pool_report(db: Session) -> list[dict]:
    """Every pool that has cards or a state row: its phase and, when
    event_logged, each card whose live counter differs from accumulator()."""
    states, _baselines, _events = _tables()
    phases = {key: phase for key, phase in db.execute(select(states.c.pool_key, states.c.logging_phase))}
    owners = {owner for (owner,) in db.query(models.PaymentCard.owner_admin_id).distinct()}
    for key in phases:
        owners.add(None if key == "global" else int(key.split(":", 1)[1]))
    report = []
    for owner in sorted(owners, key=lambda value: (value is not None, value or 0)):
        key = pool_key(owner)
        cards = db.query(models.PaymentCard).filter(models.PaymentCard.owner_admin_id == owner).all()
        drift = []
        if phases.get(key) == EVENT_LOGGED:
            for card in cards:
                expected = accumulator(db, owner, card.id)
                if expected != int(card.accumulated_amount or 0):
                    drift.append({"card_id": card.id, "live": int(card.accumulated_amount or 0), "from_events": expected})
        report.append({"owner_admin_id": owner, "pool_key": key, "phase": phases.get(key, LEGACY),
                       "cards": len(cards), "drift": drift})
    return report
