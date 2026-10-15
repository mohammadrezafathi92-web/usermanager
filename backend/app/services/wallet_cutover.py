"""Wallet cutover to the lot generation (Receipt Void P9, sections 20.2 -
20.4, 21) - and its narrow rollback.

preflight()  read-only report: everything that would stop the cutover,
             and what it would backfill. Never writes.
cutover()    ONE transaction (20.3): normal -> fencing (waits for every
             writer holding the phase), the blockers checked again, every
             User gets its identity and account (topup_blocked copied from
             purchases_blocked, 12.7), a positive balance becomes one
             legacy_baseline lot, reconciliation (lots = balance, one live
             account per User) - any difference rolls everything back -
             then -> enforced. No intermediate state is ever committed.
rollback()   only while no wallet mutation has happened since (21): no
             credit source, debit, allocation, reversal or debt. One
             transaction: enforced -> normal (epoch + 1); otherwise 409
             wallet_rollback_unavailable. The baseline lots stay (no
             financial row is ever deleted, RV-100), so a second cutover
             after a rollback is refused (wallet_recutover_unsupported).

Blockers (owner decision #7: a negative balance stops the cutover until
each case is decided by hand; it is listed, never guessed):
  wallet_phase_not_normal, ha_requires_external_fencing, negative_balances,
  provisioning_not_ready (p6_ready), provisioning_types_not_durable,
  receipt_approval_registration_off (a top-up in the lot generation is a
  receipt_topup source, which needs its approval),
  provisioning_operations_in_flight (any durable operation not yet ended),
  customer_wallet_reservations_open.
The cutover holds the phase row exclusively from its first statement; HA
on and a type back to legacy take the same row first
(wallet_service.lock_phase_for_change), so neither can cross it.
rollback() refuses after the joint activation (wallet_rollback_after_activation).
"""
from __future__ import annotations

import datetime as dt
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select, text

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import receipt_void_schema, wallet_accounts

LIST_LIMIT = 50


def _blockers(db, *, phase_expected="normal") -> tuple[list, dict]:
    from . import provisioning_readiness, receipt_approval_runtime as runtime
    blockers, info = [], {}
    state = db.execute(select(rv.wallet_runtime_state).where(rv.wallet_runtime_state.c.id == 1)).mappings().one()
    if state["phase"] != phase_expected:
        blockers.append({"code": "wallet_phase_not_normal", "phase": state["phase"]})
    panel = db.get(models.PanelSettings, 1)
    if (panel is not None and panel.ha_enabled) and state["external_fencing"] != "coordinator":
        blockers.append({"code": "ha_requires_external_fencing"})
    negative = db.query(models.User.id, models.User.username).filter(models.User.balance < 0).order_by(
        models.User.id).all()
    if negative:
        blockers.append({"code": "negative_balances", "count": len(negative),
                         "items": [{"user_id": uid, "username": name} for uid, name in negative[:LIST_LIMIT]]})
    try:
        ready, readiness = provisioning_readiness.p6_ready(db)
    except Exception:  # noqa: BLE001 - an unreadable readiness is not ready
        ready, readiness = False, [{"code": "provisioning_status_unavailable"}]
    if not ready:
        blockers.append({"code": "provisioning_not_ready", "items": readiness})
    legacy = sorted(row.operation_type for row in db.query(mp.ProvisioningTypeMode).filter(
        mp.ProvisioningTypeMode.mode != "durable"))
    if legacy:
        blockers.append({"code": "provisioning_types_not_durable", "items": legacy})
    approval = runtime.read_state(db)
    if approval is None or approval["registration_mode"] == "off":
        blockers.append({"code": "receipt_approval_registration_off"})
    if db.execute(select(func.count()).select_from(rv.wallet_lots)).scalar_one():
        # A cutover that was rolled back left its baseline lots (financial rows
        # are never deleted, RV-100); a second backfill over them is not
        # designed and is refused rather than guessed.
        blockers.append({"code": "wallet_recutover_unsupported"})
    inflight = db.execute(select(mp.ProvisioningOperation.id).where(
        mp.ProvisioningOperation.state.notin_(("completed", "compensated"))).order_by(
            mp.ProvisioningOperation.id).limit(LIST_LIMIT)).scalars().all()
    if inflight:
        # Every durable operation that has not ended - with a customer-wallet
        # hold, a reseller-credit hold or none - started in the old epoch and
        # could neither capture nor release after it (12.3, Lifecycle 8): the
        # cutover waits until each one is completed or compensated.
        blockers.append({"code": "provisioning_operations_in_flight", "items": [int(i) for i in inflight]})
    open_holds = db.execute(select(func.count()).select_from(mp.PaymentReservation.__table__).where(
        mp.PaymentReservation.payer_kind == "customer_wallet", mp.PaymentReservation.state == "reserved")).scalar_one()
    if open_holds:
        blockers.append({"code": "customer_wallet_reservations_open", "count": int(open_holds)})
    users = db.query(func.count(models.User.id)).scalar()
    with_balance = db.query(func.count(models.User.id)).filter(models.User.balance > 0).scalar()
    accounts = db.execute(select(func.count()).select_from(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id.isnot(None))).scalar_one()
    duplicates = db.query(models.User.telegram_id).filter(models.User.telegram_id.isnot(None)).group_by(
        models.User.telegram_id).having(func.count(models.User.id) > 1).count()
    info.update(users=int(users), users_with_balance=int(with_balance), accounts_existing=int(accounts),
                accounts_to_create=int(users) - int(accounts), shared_telegram_ids=int(duplicates))
    return blockers, info


def preflight(db) -> dict:
    receipt_void_schema.assert_ready()
    blockers, info = _blockers(db)
    db.rollback()
    return {"ready": not blockers, "blockers": blockers, **info}


def _begin(db):
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _lock_state(db):
    query = select(rv.wallet_runtime_state).where(rv.wallet_runtime_state.c.id == 1)
    if db.get_bind().dialect.name != "sqlite":
        query = query.with_for_update()
    return db.execute(query).mappings().one()


def _set_phase(db, current, target):
    table = rv.wallet_runtime_state
    result = db.execute(table.update().where(table.c.id == 1, table.c.phase == current).values(
        phase=target, epoch=table.c.epoch + 1, updated_at=dt.datetime.utcnow(), version=table.c.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "wallet_phase_changed")


def cutover(db) -> dict:
    receipt_void_schema.assert_ready()
    _begin(db)
    try:
        _lock_state(db)
        _set_phase(db, "normal", "fencing")      # waits for every writer of the old phase
        blockers, _info = _blockers(db, phase_expected="fencing")
        if blockers:
            raise HTTPException(409, {"error": "wallet_cutover_blocked", "blockers": blockers})
        created, baseline = _backfill(db)
        mismatches = _reconcile(db)
        if mismatches:
            raise HTTPException(409, {"error": "wallet_cutover_reconciliation_failed", "items": mismatches[:LIST_LIMIT]})
        _set_phase(db, "fencing", "enforced")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"phase": "enforced", "accounts_created": created, "baseline_lots": baseline}


def _backfill(db) -> tuple[int, int]:
    accounts, lots = rv.wallet_accounts, rv.wallet_lots
    now = dt.datetime.utcnow()
    created = baseline = 0
    for user in db.query(models.User).order_by(models.User.id).all():
        account = db.execute(select(accounts).where(accounts.c.user_id == user.id)).mappings().first()
        if account is None:
            lineage = str(uuid.uuid4())
            identity = wallet_accounts._identity(db, lineage=lineage, telegram_id=user.telegram_id,
                                                 owner_admin_id=user.owner_admin_id)
            account_id = db.execute(accounts.insert().values(
                lineage_key=lineage, customer_identity_id=identity, user_id=user.id, user_id_snapshot=user.id,
                username_snapshot=user.username[:128], owner_admin_id_snapshot=user.owner_admin_id,
                topup_blocked=bool(user.purchases_blocked),
                topup_blocked_reason=(user.purchases_blocked_reason or None) if user.purchases_blocked else None,
                created_at=now, version=0)).inserted_primary_key[0]
            created += 1
        else:
            account_id, lineage = account["id"], account["lineage_key"]
            db.execute(accounts.update().where(accounts.c.id == account_id).values(
                topup_blocked=bool(user.purchases_blocked),
                topup_blocked_reason=(user.purchases_blocked_reason or None) if user.purchases_blocked else None,
                version=accounts.c.version + 1))
        amount = int(user.balance or 0)
        if amount > 0:
            db.execute(lots.insert().values(
                wallet_account_id=account_id, source_kind="legacy_baseline", source_key=f"baseline:{lineage}",
                source_credit_source_id=None, original_amount=amount, available_amount=amount, consumed_amount=0,
                state="active", created_at=now, version=0))
            baseline += 1
    return created, baseline


def _reconcile(db) -> list:
    """Every User: Σ available of its live account = COALESCE(balance, 0);
    exactly one live account per User."""
    accounts, lots = rv.wallet_accounts, rv.wallet_lots
    totals = dict(db.execute(select(accounts.c.user_id, func.coalesce(func.sum(lots.c.available_amount), 0))
        .select_from(accounts.outerjoin(lots, (lots.c.wallet_account_id == accounts.c.id) & (lots.c.state != "voided")))
        .where(accounts.c.user_id.isnot(None)).group_by(accounts.c.user_id)).all())
    problems = []
    for user_id, balance in db.query(models.User.id, models.User.balance).all():
        if user_id not in totals:
            problems.append({"user_id": user_id, "problem": "no_account"})
        elif int(totals[user_id]) != int(balance or 0):
            problems.append({"user_id": user_id, "problem": "balance_mismatch"})
    if db.execute(select(func.count()).select_from(accounts).where(accounts.c.user_id.isnot(None))).scalar_one() != (
            db.query(func.count(models.User.id)).scalar()):
        problems.append({"problem": "account_count_mismatch"})
    return problems


MUTATION_TABLES = ("wallet_credit_sources", "wallet_debit_events", "wallet_allocations",
                   "wallet_allocation_reversals", "wallet_debts", "wallet_credit_applications")


def rollback(db) -> dict:
    receipt_void_schema.assert_ready()
    _begin(db)
    try:
        state = _lock_state(db)
        if state["phase"] != "enforced":
            raise HTTPException(409, "wallet_phase_not_enforced")
        # Lock order wallet -> approval runtime (the activation's own order):
        # after the joint activation (P9a) 'required' and 'payment_event'
        # depend on the lot wallet - there is no way back to 'normal' then.
        approval = select(rv.receipt_approval_runtime_state).where(rv.receipt_approval_runtime_state.c.id == 1)
        if db.get_bind().dialect.name != "sqlite":
            approval = approval.with_for_update(read=True)
        approval = db.execute(approval).mappings().one()
        if approval["registration_mode"] == "required" or approval["loyalty_timing_mode"] == "payment_event":
            raise HTTPException(409, "wallet_rollback_after_activation")
        used = [name for name in MUTATION_TABLES
                if db.execute(select(func.count()).select_from(getattr(rv, name))).scalar_one()]
        if used or db.execute(select(func.count()).select_from(rv.wallet_lots).where(
                rv.wallet_lots.c.source_kind != "legacy_baseline")).scalar_one():
            raise HTTPException(409, {"error": "wallet_rollback_unavailable", "used": used})
        _set_phase(db, "enforced", "normal")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    return {"phase": "normal"}


def status(db) -> dict:
    receipt_void_schema.assert_ready()
    state = db.execute(select(rv.wallet_runtime_state).where(rv.wallet_runtime_state.c.id == 1)).mappings().one()
    return {"phase": state["phase"], "epoch": int(state["epoch"]), "external_fencing": state["external_fencing"]}
