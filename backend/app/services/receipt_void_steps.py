"""The local, single-transaction steps of a receipt void (Receipt Void 13.2):
block, loyalty_reconciliation, wallet_reversal, ledger_reversal,
card_reversal, finalize. Each one runs in the caller's transaction (the
engine opened it with the phase read, the operation row and the identity
fence) and returns its result snapshot; any exception rolls the whole step
back. StepFailed / receipt_void_effects.StepBlocked carry a sanitized code.

Every step is idempotent on its own rows: effects through
mark_effect_reversed (voided_at IS NULL), ledger reversals through
UNIQUE(reversal_of_id), sources through their state, card events through
voided_at - a retry of a step that committed half never happens (one
transaction), and a retry after a commit finds the work done.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import payment_card_events, receipt_void_effects as void_effects, wallet_lots, wallet_void

LIFECYCLE = ("user_created", "purchase_created", "purchase_renewed", "connection_created")
_MODEL = {"User": models.User, "Purchase": models.Purchase, "Connection": models.Connection}


class StepFailed(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now():
    return dt.datetime.utcnow()


def _effects(db, approval_uuid):
    table = rv.receipt_approval_effects
    return [dict(r) for r in db.execute(wallet_lots._for_update(db, select(table).where(
        table.c.approval_uuid == approval_uuid).order_by(table.c.id))).mappings()]


def _locked(db, model, ident):
    if ident is None:
        return None
    return db.execute(wallet_lots._for_update(db, select(model).where(model.id == int(ident)))
                      .execution_options(populate_existing=True)).scalar_one_or_none()


# ------------------------------------------------------------------ 2. block
def block(db, ctx: dict) -> dict:
    """Fraud hold; for a whole-account scope the account disabled; every
    scope connection disabled in the DB (RADIUS reads it live). Never
    topup_blocked (13.1)."""
    user = _locked(db, models.User, ctx["user_id"])
    if user is None:
        return {"user": None}
    user.purchases_blocked = True
    user.purchases_blocked_reason = f"receipt_void:{ctx['approval_uuid']}"
    user.purchases_blocked_at = _now()
    query = select(models.Connection).where(models.Connection.user_id == user.id)
    if ctx["shape"] in ("new_user", "renew_purchase", "renew_user"):
        user.status = models.UserStatus.disabled
    elif ctx["shape"] == "existing_user":
        query = query.where(models.Connection.purchase_id == (ctx["delete"] or {}).get("id", -1))
    else:
        query = None
    disabled = []
    if query is not None:
        for connection in db.execute(wallet_lots._for_update(db, query.order_by(models.Connection.id))).scalars():
            connection.enabled = False
            disabled.append(connection.id)
    db.flush()
    return {"user": user.id, "disabled_connections": disabled}


# ------------------------------------------------------------------ 5a. loyalty_reconciliation (14.2)
def loyalty(db, ctx: dict, operation_id: int, forced: frozenset) -> dict:
    events = rv.loyalty_purchase_events
    ids = [int(r.id) for r in db.execute(select(events.c.id).where(
        events.c.approval_uuid == ctx["approval_uuid"], events.c.state == "active").order_by(events.c.id))]
    snapshot = ctx["user_id"]
    if snapshot is None:
        return {"voided_purchase_events": [], "voided_rewards": []}
    live = db.get(models.User, snapshot) is not None
    result = void_effects.reverse_loyalty(
        db, user_id=snapshot if live else None, user_id_snapshot=int(snapshot), purchase_event_ids=ids,
        operation_id=operation_id, causing_approval_uuid=ctx["approval_uuid"], sale_refund=False,
        customer_identity_id=ctx["customer_identity_id"], forced_keys=forced)
    for effect in _effects(db, ctx["approval_uuid"]):
        if effect["effect_type"] == "loyalty_purchase_recorded" and effect["voided_at"] is None:
            void_effects.mark_effect_reversed(db, effect["id"], "fully_reversed", 1, operation_id)
    return result


# ------------------------------------------------------------------ 6. wallet_reversal (12.6)
def wallet(db, ctx: dict, operation_id: int) -> dict:
    sources = rv.wallet_credit_sources
    rows = [dict(r) for r in db.execute(wallet_lots._for_update(db, select(sources).where(
        sources.c.causation_approval_uuid == ctx["approval_uuid"],
        # A loyalty reward's source is the reward's: loyalty_reconcile voids
        # it with the reward (5a) or keeps it while the entitlement stands.
        sources.c.origin_kind != "loyalty_reward").order_by(sources.c.id))).mappings()]
    for identity in sorted({wallet_lots.account(db, r["wallet_account_id"])["customer_identity_id"] for r in rows}):
        wallet_lots.fence_identity(db, identity)
    results = {}
    for source in rows:
        outcome = wallet_void.void_source(db, int(source["id"]), operation_id, ctx["customer_identity_id"])
        results[int(source["id"])] = outcome
    out = []
    for effect in _effects(db, ctx["approval_uuid"]):
        if effect["effect_type"] != "wallet_credit_source_created" or effect["voided_at"] is not None:
            continue
        if effect["effect_key"].startswith("credit:loyalty:"):
            continue                                        # 5a, or still entitled (finalize)
        if effect["resource_type"] != "WalletCreditSource":
            # A credit applied before the wallet cutover: no source to void.
            void_effects.mark_effect_reversed(db, effect["id"], "unrecoverable", 0, operation_id)
            out.append({"effect": effect["effect_key"], "state": "unrecoverable"})
            continue
        outcome = results.get(int(effect["resource_id"]))
        if outcome is None:
            state = db.execute(select(sources.c.state).where(sources.c.id == effect["resource_id"])).scalar()
            if state != "voided":
                raise StepFailed("wallet_source_not_caused_by_approval")
            outcome = wallet_void.VoidResult(int(effect["resource_id"]), True, 0, 0, 0, 0)
        void_effects.mark_effect_reversed(db, effect["id"], outcome.reversal_state, outcome.reversed_value,
                                          operation_id)
        out.append({"effect": effect["effect_key"], "state": outcome.reversal_state,
                    "reclaimed": outcome.reclaimed, "reopened": outcome.reopened, "debt": outcome.debt,
                    "writeoff": outcome.writeoff})
    return {"sources": sorted(results), "effects": out}


# ------------------------------------------------------------------ 7. ledger_reversal (14)
def _reversal_exists(db, entry_id) -> bool:
    return db.execute(select(models.LedgerEntry.id).where(models.LedgerEntry.reversal_of_id == entry_id)).first() \
        is not None


def _void_entry(db, entry_id, ctx, operation_id) -> int:
    entry = _locked(db, models.LedgerEntry, entry_id)
    if entry is None:
        raise StepFailed("ledger_entry_missing")
    if entry.voided_at is not None:
        if entry.void_operation_id == operation_id and _reversal_exists(db, entry.id):
            return int(entry.amount or 0)
        raise StepFailed("ledger_entry_voided_elsewhere")
    now = _now()
    entry.voided_at, entry.void_operation_id = now, operation_id
    db.add(models.LedgerEntry(
        kind="receipt_void", amount=0, user_id=entry.user_id if db.get(models.User, entry.user_id or 0) else None,
        username_snapshot=entry.username_snapshot, admin_id=entry.admin_id,
        admin_username_snapshot=entry.admin_username_snapshot, actor_admin_id=ctx["actor_admin_id"],
        owner_admin_id_snapshot=entry.owner_admin_id_snapshot, approval_uuid=ctx["approval_uuid"],
        void_operation_id=operation_id, reversal_of_id=entry.id, created_at=now,
        note=f"receipt void #{operation_id} of #{entry.id}"))
    db.flush()
    return int(entry.amount or 0)


def _refund_spend(db, entry_id, ctx, operation_id) -> int:
    spend = _locked(db, models.LedgerEntry, entry_id)
    if spend is None or spend.kind != "admin_credit_spend":
        raise StepFailed("ledger_entry_missing")
    amount = int(spend.amount or 0)
    if _reversal_exists(db, spend.id):
        return amount
    admin = _locked(db, models.AdminUser, spend.admin_id)
    if admin is None:
        raise StepFailed("reseller_admin_missing")
    db.execute(models.AdminUser.__table__.update().where(models.AdminUser.id == admin.id)
               .values(balance=models.AdminUser.balance + amount))
    db.add(models.LedgerEntry(
        kind="admin_credit_refund", amount=amount, user_id=spend.user_id if db.get(models.User, spend.user_id or 0)
        else None, username_snapshot=spend.username_snapshot, admin_id=admin.id,
        admin_username_snapshot=admin.username, actor_admin_id=ctx["actor_admin_id"],
        owner_admin_id_snapshot=spend.owner_admin_id_snapshot, payment_method="admin_credit",
        approval_uuid=ctx["approval_uuid"], void_operation_id=operation_id, reversal_of_id=spend.id,
        created_at=_now(), note=f"receipt void #{operation_id}: credit spend #{spend.id} refunded"))
    db.flush()
    return amount


def _void_discount(db, redemption_id, operation_id) -> Optional[int]:
    redemption = _locked(db, models.DiscountCodeRedemption, redemption_id)
    if redemption is None:
        return None
    if redemption.voided_at is None:
        redemption.voided_at, redemption.void_operation_id = _now(), operation_id
        db.flush()
    code = _locked(db, models.DiscountCode, redemption.code_id)
    if code is not None:
        code.used_count = int(db.execute(select(func.count()).select_from(models.DiscountCodeRedemption).where(
            models.DiscountCodeRedemption.code_id == code.id,
            models.DiscountCodeRedemption.voided_at.is_(None))).scalar_one())
    return int(redemption.discount_amount or 0)


def ledger(db, ctx: dict, operation_id: int, forced: frozenset) -> dict:
    out = []
    for effect in _effects(db, ctx["approval_uuid"]):
        if effect["voided_at"] is not None:
            continue
        kind, key = effect["effect_type"], effect["effect_key"]
        if kind in ("ledger_sale", "ledger_topup"):
            state, value = "fully_reversed", _void_entry(db, effect["resource_id"], ctx, operation_id)
        elif kind == "ledger_credit_spend":
            state, value = "fully_reversed", _refund_spend(db, effect["resource_id"], ctx, operation_id)
        elif kind == "discount_redeemed":
            value = _void_discount(db, effect["resource_id"], operation_id)
            state = "not_applicable" if value is None else "fully_reversed"
        elif kind == "quota_reward_granted" and not key.startswith("quota:loyalty:"):
            target_type, target_id, granted, after, unlimited = void_effects.effect_quota_facts(effect)
            state, value = void_effects.reverse_quota(
                db, target_type=target_type, target_id=target_id, granted=granted, quota_after=after,
                before_was_unlimited=unlimited, operation_id=operation_id,
                user_id_snapshot=int(ctx["user_id"] or 0), effect_id=effect["id"],
                forced=void_effects.forced(key, forced))
        elif kind in LIFECYCLE:
            if db.get(_MODEL[effect["resource_type"]], int(effect["resource_id"] or 0)) is not None:
                if ctx["delete"] is not None:
                    raise StepFailed("resource_still_present")
                state, value = "not_applicable", None
            else:
                state, value = ("resource_deleted" if ctx["delete"] is not None else "not_applicable"), None
        else:
            continue
        if state in ("resource_deleted", "not_applicable"):
            value = None
        void_effects.mark_effect_reversed(db, effect["id"], state, value, operation_id)
        out.append({"effect": f"{kind}:{key}", "state": state, "value": value})
    return {"effects": out}


# ------------------------------------------------------------------ 8. card_reversal (15.5)
def card(db, ctx: dict, operation_id: int) -> dict:
    states, _baselines, events = payment_card_events._tables()
    event = db.execute(select(events).where(events.c.approval_uuid == ctx["approval_uuid"])).mappings().first()
    effect = next((e for e in _effects(db, ctx["approval_uuid"]) if e["effect_type"] == "card_payment_recorded"), None)
    if event is None:
        if effect is not None and effect["voided_at"] is None:
            void_effects.mark_effect_reversed(db, effect["id"], "not_applicable", None, operation_id)
        return {"note": "no_card_event"}
    db.execute(select(states.c.logging_phase).where(states.c.pool_key == event["pool_key"]).with_for_update()).all()
    event = db.execute(select(events).where(events.c.id == event["id"]).with_for_update()).mappings().one()
    if event["voided_at"] is None:
        db.execute(events.update().where(events.c.id == event["id"], events.c.voided_at.is_(None)).values(
            voided_at=_now(), void_operation_id=operation_id))
    card_row = _locked(db, models.PaymentCard, event["card_id"])
    counter = None
    if card_row is not None and card_row.id == event["card_id_snapshot"]:
        old = int(card_row.accumulated_amount or 0)
        owner = None if event["pool_key"] == "global" else int(event["pool_key"].split(":", 1)[1])
        counter = payment_card_events.accumulator(db, owner, card_row.id)
        changed = db.execute(models.PaymentCard.__table__.update().where(
            models.PaymentCard.id == card_row.id, models.PaymentCard.accumulated_amount == old).values(
            accumulated_amount=counter)).rowcount
        if changed != 1:
            raise StepFailed("card_counter_changed")
        db.expire(card_row, ["accumulated_amount"])
    if effect is not None and effect["voided_at"] is None:
        void_effects.mark_effect_reversed(db, effect["id"], "fully_reversed", int(event["amount"]), operation_id)
    return {"event_id": int(event["id"]), "card_counter": counter}


# ------------------------------------------------------------------ 9. finalize
def finalize(db, ctx: dict, operation_id: int) -> dict:
    rewards = rv.loyalty_reward_events
    open_effects = []
    for effect in _effects(db, ctx["approval_uuid"]):
        if effect["voided_at"] is not None:
            continue
        loyalty_key = effect["effect_key"].startswith(("credit:loyalty:", "quota:loyalty:"))
        if loyalty_key:
            # The reward this approval earned is still backed by the count
            # (14.2): nothing of it is reversed, and it stays the reward's.
            _kind, _loyalty, epoch, slot = effect["effect_key"].split(":")
            active = db.execute(select(rewards.c.id).where(
                rewards.c.causation_approval_uuid == ctx["approval_uuid"], rewards.c.epoch_id == int(epoch),
                rewards.c.slot == int(slot), rewards.c.state == "active")).first()
            if active is not None:
                void_effects.mark_effect_reversed(db, effect["id"], "not_applicable", None, operation_id)
                continue
        open_effects.append(f"{effect['effect_type']}:{effect['effect_key']}")
    if open_effects:
        raise StepFailed("effects_not_reversed")
    return {"effects_reversed": True}
