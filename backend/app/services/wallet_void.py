"""The receipt-void half of the lot wallet (Receipt Void 12.5, 12.6): V
(voiding the credit sources an approval caused) and R2 (a refund over a
lot that a void already voided). Lot generation only ('enforced'); every
function works in the caller's transaction and never commits.

V(source):  the source goes voiding; every debt settlement it made is
            reversed as a void write-off (the debt it settled re-opens);
            every lot it still has is voided - what was left is clawed
            back, what was consumed becomes a debt of the approval's own
            customer identity, or a wallet_writeoffs row for anyone else
            (a third party's reward, owner default #4); the source is voided.
R2(alloc):  the scam money a refunded sale spent never becomes spendable:
            the debt that lot created is adjusted down by the refunded
            amount and only a settlement in excess of the new obligation is
            freed, latest first, and re-distributed (never onto the same debt).
No financial row is ever deleted (RV-100).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import select

from .. import models_receipt_void as rv
from . import wallet_lots


def _now():
    return dt.datetime.utcnow()


def _lock(db, query):
    return wallet_lots._for_update(db, query)


def _debt_state(amount, adjusted, settled):
    return wallet_lots._debt_state(int(amount), int(adjusted), int(settled))


@dataclass(frozen=True)
class VoidResult:
    source_id: int
    already_voided: bool
    reclaimed: int          # available money of its lots taken back
    reopened: int           # debt settlements it had made, reversed
    debt: int               # consumed by the approval's own identity -> new debts
    writeoff: int           # consumed by another identity -> write-offs

    @property
    def reversal_state(self) -> str:
        if self.debt > 0:
            return "reversed_with_debt"
        if self.writeoff > 0:
            return "reversed_with_writeoff"
        return "fully_reversed"

    @property
    def reversed_value(self) -> int:
        return self.reclaimed + self.reopened


def void_source(db, source_id: int, operation_id: int, customer_identity_id: int | None) -> VoidResult:
    """V (12.6) for one source. A source already voided is reported, never
    voided twice (retry of the step)."""
    sources, apps, lots, debts = (rv.wallet_credit_sources, rv.wallet_credit_applications, rv.wallet_lots,
                                  rv.wallet_debts)
    source = db.execute(_lock(db, select(sources).where(sources.c.id == source_id))).mappings().one()
    if source["state"] == "voided":
        return VoidResult(source_id, True, 0, 0, 0, 0)
    if source["state"] not in ("applied", "voiding"):
        raise HTTPException(409, "wallet_source_not_applied")
    now = _now()
    db.execute(sources.update().where(sources.c.id == source_id).values(
        state="voiding", voided_at=now, void_operation_id=operation_id, version=sources.c.version + 1))
    touched = {int(source["wallet_account_id"])}
    reopened = 0
    for app in db.execute(_lock(db, select(apps).where(
            apps.c.wallet_credit_source_id == source_id, apps.c.application_kind == "debt_settlement").order_by(
                apps.c.id))).mappings().all():
        effective = int(app["amount"]) - int(app["reversed_amount"])
        if effective <= 0:
            continue
        db.execute(rv.wallet_credit_application_reversals.insert().values(
            wallet_credit_application_id=app["id"], effect_key=f"void:{operation_id}", amount=effective,
            disposition="void_writeoff", wallet_operation_id=operation_id, created_at=now))
        db.execute(apps.update().where(apps.c.id == app["id"]).values(
            reversed_amount=app["amount"], version=apps.c.version + 1))
        debt = db.execute(_lock(db, select(debts).where(debts.c.id == app["wallet_debt_id"]))).mappings().one()
        settled = int(debt["settled_amount"]) - effective
        db.execute(debts.update().where(debts.c.id == debt["id"], debts.c.version == debt["version"]).values(
            settled_amount=settled, state=_debt_state(debt["amount"], debt["adjusted_amount"], settled),
            version=debts.c.version + 1))
        reopened += effective
    reclaimed = owed = written = 0
    for lot in db.execute(_lock(db, select(lots).where(
            lots.c.source_credit_source_id == source_id, lots.c.state != "voided").order_by(lots.c.id))).mappings().all():
        reclaimed += int(lot["available_amount"])
        db.execute(lots.update().where(lots.c.id == lot["id"], lots.c.version == lot["version"]).values(
            voided_available_amount=lot["available_amount"], available_amount=0, state="voided", voided_at=now,
            void_operation_id=operation_id, version=lots.c.version + 1))
        touched.add(int(lot["wallet_account_id"]))
        consumed = int(lot["consumed_amount"])
        if consumed <= 0:
            continue
        account = wallet_lots.account(db, lot["wallet_account_id"])
        if customer_identity_id is not None and account["customer_identity_id"] == customer_identity_id:
            db.execute(debts.insert().values(
                customer_identity_id=account["customer_identity_id"], wallet_account_id=account["id"],
                source_wallet_lot_id=lot["id"], source_void_operation_id=operation_id, amount=consumed,
                adjusted_amount=0, settled_amount=0, state="open", created_at=now, version=0))
            owed += consumed
        else:
            db.execute(rv.wallet_writeoffs.insert().values(
                wallet_operation_id=operation_id, customer_identity_id=account["customer_identity_id"],
                wallet_account_id=account["id"], wallet_lot_id=lot["id"], amount=consumed,
                reason="third_party_reward_consumed", created_at=now))
            written += consumed
    db.execute(sources.update().where(sources.c.id == source_id).values(
        state="voided", unapplied_amount=0, version=sources.c.version + 1))
    for account_id in sorted(touched):
        account = wallet_lots.account(db, account_id)
        if account["user_id"] is not None:
            wallet_lots.write_cache(db, account)
    return VoidResult(source_id, False, reclaimed, reopened, owed, written)


def refund_voided_allocation(db, allocation: dict, lot: dict, operation_id: int) -> None:
    """R2 (12.5): an allocation over a voided lot. The reversal row always;
    with a debt from that lot, its obligation goes down by the refund and
    only the excess settlement is freed and re-distributed elsewhere."""
    debts, apps = rv.wallet_debts, rv.wallet_credit_applications
    amount = int(allocation["allocated_amount"])
    now = _now()
    rev = db.execute(rv.wallet_allocation_reversals.insert().values(
        wallet_allocation_id=allocation["id"], reversed_amount=amount, lot_was_voided=True,
        wallet_operation_id=operation_id, created_at=now)).inserted_primary_key[0]
    debt = db.execute(_lock(db, select(debts).where(debts.c.source_wallet_lot_id == lot["id"]))).mappings().first()
    if debt is None:
        return                                    # a third party's lot: its write-off stays as it is
    new_obligation = int(debt["amount"]) - (int(debt["adjusted_amount"]) + amount)
    if new_obligation < 0:
        raise HTTPException(409, "wallet_refund_exceeds_debt")
    excess = max(int(debt["settled_amount"]) - new_obligation, 0)
    settled = int(debt["settled_amount"])
    freed = []
    for app in db.execute(_lock(db, select(apps).where(
            apps.c.wallet_debt_id == debt["id"], apps.c.application_kind == "debt_settlement").order_by(
                apps.c.created_at.desc(), apps.c.id.desc()))).mappings().all():
        effective = int(app["amount"]) - int(app["reversed_amount"])
        take = min(effective, excess)
        if take <= 0:
            if excess <= 0:
                break
            continue
        db.execute(rv.wallet_credit_application_reversals.insert().values(
            wallet_credit_application_id=app["id"], effect_key=f"refund:{rev}", amount=take,
            disposition="reallocate", wallet_operation_id=operation_id, created_at=now))
        db.execute(apps.update().where(apps.c.id == app["id"]).values(
            reversed_amount=apps.c.reversed_amount + take, version=apps.c.version + 1))
        settled -= take
        excess -= take
        freed.append((int(app["wallet_credit_source_id"]), take))
    adjusted = int(debt["adjusted_amount"]) + amount
    db.execute(rv.wallet_debt_adjustments.insert().values(
        wallet_debt_id=debt["id"], source_allocation_reversal_id=rev, amount=amount,
        wallet_operation_id=operation_id, created_at=now))
    # The freed settlement (3) and the adjustment (4) land in ONE statement:
    # the debt's CHECKs (settled <= amount - adjusted, the state shape) are
    # checked on the final row.
    db.execute(debts.update().where(debts.c.id == debt["id"], debts.c.version == debt["version"]).values(
        settled_amount=settled, adjusted_amount=adjusted, state=_debt_state(debt["amount"], adjusted, settled),
        version=debts.c.version + 1))
    sources = rv.wallet_credit_sources
    for source_id, amount_freed in freed:
        db.execute(sources.update().where(sources.c.id == source_id).values(
            unapplied_amount=sources.c.unapplied_amount + amount_freed, state="applying",
            version=sources.c.version + 1))
        source = dict(db.execute(select(sources).where(sources.c.id == source_id)).mappings().one())
        account = wallet_lots.account(db, source["wallet_account_id"])
        wallet_lots._distribute(db, source, account, operation_id, exclude_debt=debt["id"],
                                effect_prefix=f"realloc:{rev}:")
        if account["user_id"] is not None:
            wallet_lots.write_cache(db, account)
