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


def advance_after_payment(db: Session, card_id: int, amount: int) -> None:
    """Called once a card-to-card payment/top-up is actually CONFIRMED (an
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
    make that approval fail."""
    if amount <= 0:
        return
    card = db.get(models.PaymentCard, card_id)
    if not card:
        return
    card.accumulated_amount = (card.accumulated_amount or 0) + amount

    if card.owner_admin_id is None:
        settings = db.get(models.PanelSettings, 1)
        mode = (settings.payment_card_mode if settings else None) or "manual"
        threshold = settings.payment_card_switch_threshold if settings else None
        is_active_pointer = bool(settings and settings.active_payment_card_id == card.id)
    else:
        admin = db.get(models.AdminUser, card.owner_admin_id)
        mode = (admin.own_payment_card_mode if admin else None) or "manual"
        threshold = admin.own_payment_card_switch_threshold if admin else None
        is_active_pointer = bool(admin and admin.own_active_payment_card_id == card.id)

    if mode == "threshold" and threshold and is_active_pointer and card.accumulated_amount >= threshold:
        cards = _pool_query(db, card.owner_admin_id).all()
        if len(cards) > 1:
            idx = next((i for i, c in enumerate(cards) if c.id == card.id), 0)
            next_card = cards[(idx + 1) % len(cards)]
            if card.owner_admin_id is None:
                settings.active_payment_card_id = next_card.id
            else:
                admin.own_active_payment_card_id = next_card.id
        card.accumulated_amount = 0

    db.commit()
