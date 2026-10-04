"""Card-to-card payment card pool: an admin can register several bank
cards - either in the panel-wide GLOBAL pool (owner_admin_id=NULL, backs
the shared bot and every admin without their own dedicated bot - see
models.PanelSettings.payment_card_mode/active_payment_card_id) or in one
specific Admin's/Seller's OWN pool for their dedicated bot
(owner_admin_id=that admin's id - see
models.AdminUser.own_payment_card_mode/own_active_payment_card_id) - and
choose how the panel picks which one to show a customer right now.

Added because a single card taking too many small card-to-card transfers
in a short window is a common trigger for Iranian banks freezing it -
spreading deposits across several registered cards (manually, on a timer,
or once a card has taken "enough" for now) is a real operational need for
a panel selling many small subscriptions.

Three modes (same string value on both PanelSettings.payment_card_mode and
AdminUser.own_payment_card_mode):
  "manual"    - always the pool's own active_payment_card_id card, until an
                admin explicitly picks a different one.
  "rotate"    - every time a customer reaches the payment screen, hands out
                the LEAST RECENTLY shown active card in the pool (round-
                robin) - see resolve_active_card.
  "threshold" - same as "manual" for display (resolve_active_card doesn't
                re-decide on every view), but the panel tracks confirmed
                deposits against the active card (record_payment, called
                once a receipt/top-up paid to that card is actually
                APPROVED - see telegram_bot/handlers/admin_pending.py) and
                auto-advances to the next card in the pool once the active
                card's accumulated total reaches the configured switch
                threshold - see advance_after_payment.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy.orm import Session

from .. import models
from . import payment_card_events

VALID_MODES = ("manual", "rotate", "threshold")


def _pool_query(db: Session, owner_admin_id: Optional[int]):
    return (
        db.query(models.PaymentCard)
        .filter(models.PaymentCard.owner_admin_id == owner_admin_id, models.PaymentCard.is_active.is_(True))
        .order_by(models.PaymentCard.sort_order, models.PaymentCard.id)
    )


def list_cards(db: Session, owner_admin_id: Optional[int]) -> list[models.PaymentCard]:
    """EVERY card in this pool, active or not - for the admin's own card-
    management list (unlike _pool_query above, which only ever returns
    is_active cards since that's what resolution/rotation should pick
    from). An admin needs to see a deactivated card to re-activate or
    delete it, not just the ones currently eligible to be shown."""
    return (
        db.query(models.PaymentCard)
        .filter(models.PaymentCard.owner_admin_id == owner_admin_id)
        .order_by(models.PaymentCard.sort_order, models.PaymentCard.id)
        .all()
    )


def resolve_active_card(
    db: Session, owner_admin_id: Optional[int], mode: str, active_card_id: Optional[int],
) -> Optional[models.PaymentCard]:
    """Returns the PaymentCard that should be shown right now for this pool
    (owner_admin_id=None -> global pool, otherwise one admin's own pool).
    None if the pool has no active cards at all - the caller (routers/
    bot.py's get_payment_info) falls back to the legacy single
    payment_card_number/holder field in that case."""
    cards = _pool_query(db, owner_admin_id).all()
    if not cards:
        return None

    if mode == "rotate":
        # Least-recently-used wins - NULL (never used yet) sorts first so a
        # freshly-added card enters the rotation immediately instead of
        # waiting behind every already-used card.
        chosen = min(cards, key=lambda c: c.last_used_at or dt.datetime.min)
        chosen.last_used_at = dt.datetime.utcnow()
        db.commit()
        return chosen

    # "manual" and "threshold" both just show whatever's currently marked
    # active - "threshold" only ever moves that pointer on its own via
    # advance_after_payment below, never just because the screen was viewed.
    if active_card_id:
        match = next((c for c in cards if c.id == active_card_id), None)
        if match:
            return match
    # active_*_card_id unset or stale (e.g. that card was deactivated/
    # deleted) - fall back to the first card in the pool rather than
    # showing nothing.
    return cards[0]


def advance_after_payment_core(db: Session, card_id: int, amount: int, approval_uuid: Optional[str] = None) -> bool:
    """The mutation of advance_after_payment WITHOUT its commit - for a
    caller that records the payment inside a larger transaction (Lifecycle/P6
    design 7.3). Same logic, same best-effort no-ops. Returns whether
    anything was written (False for the two silent no-ops).

    Called once a card-to-card payment/top-up is actually CONFIRMED (an
    admin approves the receipt - see telegram_bot/handlers/admin_pending.py)
    for a specific card. Always records the amount (see
    PaymentCard.accumulated_amount's docstring for why this happens
    regardless of mode); if the card's pool is in "threshold" mode, this
    card is still the pool's active one, and the running total has now
    reached the configured switch threshold, advances the pool's active-
    card pointer to the next card (by sort_order/id, wrapping around) and
    resets this card's total back to 0.

    Best-effort by design - amount<=0 or a since-deleted card is a silent
    no-op rather than an error, since this is bookkeeping on the side of an
    already-successful approval, never something that should be able to
    make that approval fail.

    For a pool whose logging phase is 'event_logged' (Receipt Void design
    15.2) the same transaction also writes the payment's event row; a
    'legacy' pool behaves exactly as it always did."""
    if amount <= 0:
        return False
    card = db.get(models.PaymentCard, card_id)
    if not card:
        return False
    logged = payment_card_events.phase_for_payment(db, card.owner_admin_id) == payment_card_events.EVENT_LOGGED
    if logged:
        # the pool is locked now: take the counter as committed, not as this
        # session happened to read it earlier in the request
        db.refresh(card, with_for_update=True)
    accumulated_before = card.accumulated_amount or 0
    card.accumulated_amount = accumulated_before + amount

    holder, mode_attr, pointer_attr, threshold_attr = _pool_holder(db, card.owner_admin_id, create=False)
    mode = (getattr(holder, mode_attr) if holder else None) or "manual"
    threshold = getattr(holder, threshold_attr) if holder else None
    active_before = getattr(holder, pointer_attr) if holder else None
    is_active_pointer = bool(holder and active_before == card.id)

    reset_after, rotated_to, card_order = False, None, None
    if mode == "threshold" and threshold and is_active_pointer and card.accumulated_amount >= threshold:
        cards = _pool_query(db, card.owner_admin_id).all()
        card_order = [c.id for c in cards]
        if len(cards) > 1:
            idx = next((i for i, c in enumerate(cards) if c.id == card.id), 0)
            next_card = cards[(idx + 1) % len(cards)]
            setattr(holder, pointer_attr, next_card.id)
            rotated_to = next_card.id
        card.accumulated_amount = 0
        reset_after = True
    if logged:
        if card_order is None:
            card_order = [c.id for c in _pool_query(db, card.owner_admin_id).all()]
        payment_card_events.record_payment(
            db, card, amount=amount, accumulated_before=accumulated_before, active_card_id_before=active_before,
            card_order=card_order, reset_after=reset_after, rotated_to_card_id=rotated_to, mode=mode,
            threshold=threshold, approval_uuid=approval_uuid)
    return True


def advance_after_payment(db: Session, card_id: int, amount: int) -> None:
    """advance_after_payment_core, then one commit if (and only if) it wrote
    something - exactly what this function always did, and what every
    existing caller relies on (routers/bot.py's record_card_payment)."""
    if not advance_after_payment_core(db, card_id, amount):
        return
    db.commit()


# --------------------------------------------------------------------------
# Every OTHER write to a card pool. (Receipt Void design, phase P1: all
# writers of PaymentCard rows and of the three per-pool settings - mode,
# active-card pointer, switch threshold - live in this module, so that the
# later phases can put the pool lock and the causal event log in exactly one
# place. tests/test_payment_card_service.py fails if a writer appears
# anywhere else.)
#
# None of these commits: the calling endpoint does, exactly as before, so
# each request is still one transaction and behaves as it always did.
# owner_admin_id=None is the global pool, otherwise that admin's own pool.
_UNSET = object()


def _pool_holder(db: Session, owner_admin_id: Optional[int], create: bool = True):
    """(row, mode attr, pointer attr, threshold attr) holding this pool's
    three settings: the PanelSettings singleton or the owning AdminUser."""
    if owner_admin_id is None:
        row = db.get(models.PanelSettings, 1)
        if row is None and create:
            row = models.PanelSettings(id=1)
            db.add(row)
            db.flush()
        return row, "payment_card_mode", "active_payment_card_id", "payment_card_switch_threshold"
    admin = db.get(models.AdminUser, owner_admin_id)
    return admin, "own_payment_card_mode", "own_active_payment_card_id", "own_payment_card_switch_threshold"


def set_pool_settings(db: Session, owner_admin_id: Optional[int], *, mode=_UNSET, active_card_id=_UNSET,
                      switch_threshold=_UNSET) -> None:
    """Writes whichever of the pool's three settings were given - verbatim,
    no validation beyond what the caller already did."""
    holder, mode_attr, pointer_attr, threshold_attr = _pool_holder(db, owner_admin_id)
    if mode is not _UNSET:
        setattr(holder, mode_attr, mode)
    if active_card_id is not _UNSET:
        setattr(holder, pointer_attr, active_card_id)
    if switch_threshold is not _UNSET:
        setattr(holder, threshold_attr, switch_threshold)


def create_card(db: Session, owner_admin_id: Optional[int], fields: dict) -> models.PaymentCard:
    """Adds a card to the pool. The first card of an empty pool becomes the
    active one right away (otherwise the pointer would stay NULL and the
    legacy single-card field would keep being shown)."""
    was_empty = not list_cards(db, owner_admin_id)
    card = models.PaymentCard(owner_admin_id=owner_admin_id, **fields)
    db.add(card)
    db.flush()
    if was_empty:
        set_pool_settings(db, owner_admin_id, active_card_id=card.id)
        payment_card_events.start_new_pool(db, owner_admin_id)
    return card


def update_card(db: Session, card: models.PaymentCard, fields: dict) -> models.PaymentCard:
    for key, value in fields.items():
        setattr(card, key, value)
    return card


def delete_card(db: Session, card: models.PaymentCard) -> None:
    """Deletes the card; if it was the pool's active one, the pointer moves
    to whatever is left (None for an emptied pool) instead of dangling."""
    owner_admin_id, card_id = card.owner_admin_id, card.id
    holder, _mode_attr, pointer_attr, _threshold_attr = _pool_holder(db, owner_admin_id)
    payment_card_events.detach_card(db, owner_admin_id, card_id)
    db.delete(card)
    db.flush()
    if getattr(holder, pointer_attr) == card_id:
        remaining = list_cards(db, owner_admin_id)
        setattr(holder, pointer_attr, remaining[0].id if remaining else None)


def activate_card(db: Session, card: models.PaymentCard) -> None:
    set_pool_settings(db, card.owner_admin_id, active_card_id=card.id)
