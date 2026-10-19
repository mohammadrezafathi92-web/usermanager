"""Read-only reconciliation of the lot wallet and payment-time loyalty
(Receipt Void 20.5, 14.2): alerts only, never a repair (a cache repair is
its own wallet_cache_repair operation, not implemented here).

  cache_mismatch      User.balance != Σ available of its live account (I-CACHE)
  user_without_account  a User with no live account (after the cutover)
  stale_hold          a sale debit still 'held' after HOLD_ALERT_MINUTES
                      (owner decision #11 default: 15 minutes) - never
                      released automatically, the service may exist
  open_epoch          payment_event needs exactly one open epoch of the
                      current generation; legacy_activation none
  loyalty_cache       purchase_count / loyalty_rewards_given != the events
  cutover_snapshot_*  a P9a reservation snapshot changed, or its live purchase
                      consumed past it with no logged consumption (a deleted
                      purchase, or a reused id, is history - no alert)
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import loyalty_service, receipt_void_schema, reservation_transition

HOLD_ALERT_MINUTES = 15
LIMIT = 50


def check(db) -> dict:
    receipt_void_schema.assert_ready()
    problems = []
    phase = db.execute(select(rv.wallet_runtime_state.c.phase).where(rv.wallet_runtime_state.c.id == 1)).scalar()
    if phase == "enforced":
        accounts, lots = rv.wallet_accounts, rv.wallet_lots
        totals = dict(db.execute(select(accounts.c.user_id, func.coalesce(func.sum(lots.c.available_amount), 0))
            .select_from(accounts.outerjoin(lots, (lots.c.wallet_account_id == accounts.c.id) & (lots.c.state != "voided")))
            .where(accounts.c.user_id.isnot(None)).group_by(accounts.c.user_id)).all())
        for user_id, balance in db.query(models.User.id, models.User.balance).all():
            if user_id not in totals:
                problems.append({"code": "user_without_account", "user_id": user_id})
            elif int(totals[user_id]) != int(balance or 0):
                problems.append({"code": "cache_mismatch", "user_id": user_id})
        cutoff = dt.datetime.utcnow() - dt.timedelta(minutes=HOLD_ALERT_MINUTES)
        for row in db.execute(select(rv.wallet_debit_events.c.id, rv.wallet_debit_events.c.provisioning_ref).where(
                rv.wallet_debit_events.c.state == "held", rv.wallet_debit_events.c.held_at < cutoff)).all():
            problems.append({"code": "stale_hold", "debit_id": row.id, "provisioning_ref": row.provisioning_ref})
    state = loyalty_service.runtime_row(db)
    if state is not None:
        epochs = db.execute(select(rv.loyalty_policy_epochs.c.generation).where(
            rv.loyalty_policy_epochs.c.closed_at.is_(None))).scalars().all()
        if state["loyalty_timing_mode"] == "payment_event":
            if epochs != [int(state["completeness_generation"])]:
                problems.append({"code": "open_epoch", "open_epochs": len(epochs)})
            generation = int(state["completeness_generation"])
            for user in db.query(models.User).all():
                if (int(user.purchase_count or 0), int(user.loyalty_rewards_given or 0)) != (
                        loyalty_service.active_count(db, user.id, generation),
                        loyalty_service.live_rewards(db, user.id, generation)):
                    problems.append({"code": "loyalty_cache", "user_id": user.id})
        elif epochs:
            problems.append({"code": "open_epoch", "open_epochs": len(epochs)})
    problems.extend(reservation_transition.check(db))
    db.rollback()
    return {"ok": not problems, "count": len(problems), "problems": problems[:LIMIT]}
