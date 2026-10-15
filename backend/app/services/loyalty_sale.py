"""A durable sale's loyalty, in its T_final (Receipt Void P9a, 14.2).

Called from provisioning_finalization.finish() after the sale's LedgerEntry
and the reservations' capture, in the same transaction, only while
loyalty_timing_mode is 'payment_event'. The sale is eligible only with
backend-owned payment evidence (owner decision #16 - the design default):
  receipt_approval  a 'required' approval of this generation, mutating, kind
                    new/renew, same tenant and customer, its amount = the
                    sale's amount > 0;
  wallet_capture    the customer's wallet reservation of THIS operation,
                    captured with this sale's LedgerEntry, same amount.
Anything else (reseller credit, a card sale without approval, a trial, a
zero price) records nothing. The caches purchase_count /
loyalty_rewards_given are rewritten from the events either way.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import select

from .. import models, models_receipt_void as rv
from . import loyalty_service, wallet_service


def _evidence(db, operation, user, entry, reservations, approval_row):
    if entry is None or int(entry.amount or 0) <= 0 or entry.kind not in ("sale_new", "sale_renew"):
        return None
    state = loyalty_service.runtime_row(db, lock=True)
    wallet = next((row for row in reservations if row.payer_kind == "customer_wallet"
                   and row.generation == "wallet_lot" and row.state == "captured"), None)
    if wallet is not None:
        debit = db.execute(select(rv.wallet_debit_events.c.id, rv.wallet_debit_events.c.wallet_account_id).where(
            rv.wallet_debit_events.c.hold_ref == wallet.hold_ref)).one()
        if int(wallet.amount) != int(entry.amount):
            return None
        evidence = loyalty_service.record_evidence(
            db, kind="wallet_capture", sale_entry=entry, user=user, tenant_scope_key=operation.tenant_scope_key,
            debit_id=debit.id, wallet_account_id=debit.wallet_account_id, captured_amount=int(wallet.amount))
        return evidence, "wallet_capture", None, debit.id
    if approval_row is None:
        return None
    if (approval_row["registered_under_mode"] != "required" or approval_row["state"] != "mutating"
            or int(approval_row["completeness_generation"] or 0) != int(state["completeness_generation"])
            or approval_row["kind"] not in ("new", "renew")
            or approval_row["tenant_scope_key"] != operation.tenant_scope_key
            or int(approval_row["amount_snapshot"]) != int(entry.amount)):
        return None
    if approval_row["target_user_id_snapshot"] is not None and approval_row["target_user_id_snapshot"] != user.id:
        return None
    evidence = loyalty_service.record_evidence(
        db, kind="receipt_approval", sale_entry=entry, user=user, tenant_scope_key=operation.tenant_scope_key,
        approval_uuid=approval_row["approval_uuid"], captured_amount=int(approval_row["amount_snapshot"]))
    return evidence, "receipt_approval", approval_row["approval_uuid"], None


def record_sale(db, operation, user: models.User, entry, reservations, approval_row) -> Optional[dict]:
    """The loyalty of one sale. Returns what the approval's effects need
    (None when nothing was recorded)."""
    if not loyalty_service.payment_event_mode(db):
        return None
    found = _evidence(db, operation, user, entry, reservations, approval_row)
    state = loyalty_service.runtime_row(db)
    generation = int(state["completeness_generation"])
    if found is None:
        loyalty_service.write_caches(db, user, generation)
        return None
    evidence_id, causation, approval_uuid, debit_id = found
    credit_evidence = {}

    def on_credit(customer, amount, source_key):
        before = int(db.execute(select(models.User.__table__.c.balance).where(
            models.User.__table__.c.id == customer.id)).scalar() or 0)
        after = wallet_service.credit_reward(db, customer, amount, source_kind=wallet_service.LOYALTY_REWARD,
                                             source_key=source_key, causation_approval_uuid=approval_uuid)
        credit_evidence.update(before=before, after=after)
        return db.execute(select(rv.wallet_credit_sources.c.id).where(
            rv.wallet_credit_sources.c.origin_kind == "loyalty_reward",
            rv.wallet_credit_sources.c.source_key == source_key)).scalar()

    recorded = loyalty_service.record_purchase(db, user, entry, evidence_id, causation=causation,
                                               approval_uuid=approval_uuid, debit_id=debit_id, on_credit=on_credit)
    user._loyalty_reward_just_granted = ((recorded.credit, recorded.quota_granted / 1024 ** 3)
                                         if recorded.slot is not None else None)
    return {"recorded": recorded, "causation": causation, "credit_before": credit_evidence.get("before"),
            "credit_after": credit_evidence.get("after")}
