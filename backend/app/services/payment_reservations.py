"""DB-only payment holds for Lifecycle 8; no live caller or activation yet.

Caller: short BEGIN IMMEDIATE on SQLite, canonical runtime/lease locks on
MariaDB, and the operation transaction. Never call these around network I/O.
No function commits/rolls back. A caller must roll back ANY failure, including
an INSERT/UNIQUE failure after deduction; retry the whole transaction.

Only pre-cutover legacy_balance/admin_balance are implemented. wallet_lot
is deliberately refused until the source/lot writer exists.
"""
import datetime as dt
import uuid

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import accounting, admin_billing, provisioning_schema, receipt_void_schema
from . import wallet_policy, wallet_service


def _operation(db, operation_id):
    if not provisioning_schema.is_ready() or not receipt_void_schema.is_ready():
        raise HTTPException(503, "payment_reservation_schema_unavailable")
    wallet_service.require_legacy_phase(db)
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).scalar_one()
    operation = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.id == operation_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if operation is None:
        raise HTTPException(404, "provisioning_operation_not_found")
    if operation.wallet_epoch_at_start != epoch:
        raise HTTPException(409, "wallet_epoch_changed")
    return operation


def reserve(db, operation_id, payer_kind, payer_id, amount):
    if payer_kind not in ("customer_wallet", "reseller_credit") or type(amount) is not int or amount <= 0:
        raise HTTPException(422, "invalid_payment_reservation")
    operation = _operation(db, operation_id)
    existing = db.execute(select(mp.PaymentReservation).where(
        mp.PaymentReservation.operation_id == operation.id,
        mp.PaymentReservation.payer_kind == payer_kind).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    generation = "legacy_balance" if payer_kind == "customer_wallet" else "admin_balance"
    if existing is not None:
        actual_id = existing.user_id if payer_kind == "customer_wallet" else existing.admin_id
        if (actual_id, existing.amount, existing.generation) != (payer_id, amount, generation):
            raise HTTPException(409, "payment_reservation_conflict")
        return existing
    if operation.state != "prepared":
        raise HTTPException(409, "payment_reservation_operation_state")
    if payer_kind == "customer_wallet":
        payer = db.execute(select(models.User).where(models.User.id == payer_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if payer is None or operation.target_user_id not in (None, payer_id):
            raise HTTPException(409, "payment_reservation_payer_mismatch")
        wallet_policy.can_purchase(db, payer)
        if not wallet_service.debit_atomic(db, payer_id, amount, source_kind=wallet_service.SALE):
            raise HTTPException(400, "موجودی کیف پول کافی نیست")
    else:
        payer = db.execute(select(models.AdminUser).where(models.AdminUser.id == payer_id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if payer is None:
            raise HTTPException(404, "payment_reservation_payer_not_found")
        if payer.is_superadmin or payer.billing_mode == "usage":
            raise HTTPException(422, "payment_reservation_payer_exempt")
        if not admin_billing.reserve_admin_balance_core(db, payer, amount):
            raise HTTPException(400, "اعتبار فروشنده کافی نیست")
    row = mp.PaymentReservation(operation_id=operation.id, payer_kind=payer_kind,
        generation=generation, user_id=payer_id if payer_kind == "customer_wallet" else None,
        admin_id=payer_id if payer_kind == "reseller_credit" else None, amount=amount,
        hold_ref=str(uuid.uuid4()), state="reserved", reserved_at=dt.datetime.utcnow())
    db.add(row)
    db.flush()
    return row


def _reservation(db, reservation_id):
    # Read just the operation reference first; all mutations then serialize
    # through that operation before locking a reservation or payer.
    operation_id = db.execute(select(mp.PaymentReservation.operation_id).where(
        mp.PaymentReservation.id == reservation_id)).scalar_one_or_none()
    if operation_id is None:
        raise HTTPException(404, "payment_reservation_not_found")
    operation = _operation(db, operation_id)
    row = db.execute(select(mp.PaymentReservation).where(
        mp.PaymentReservation.id == reservation_id).with_for_update().execution_options(populate_existing=True)).scalar_one()
    if row.generation not in ("legacy_balance", "admin_balance"):
        raise HTTPException(503, "payment_reservation_generation_unavailable")
    return operation, row


def capture(db, reservation_id, *, sale_ledger_entry_id=None, package=None):
    operation, row = _reservation(db, reservation_id)
    if row.state == "released":
        raise HTTPException(409, "payment_reservation_already_released")
    if row.state == "captured":
        if row.payer_kind == "customer_wallet" and row.capture_ledger_entry_id != sale_ledger_entry_id:
            raise HTTPException(409, "payment_reservation_conflict")
        return row
    if operation.state != "remote_complete" and not (
        operation.state == "prepared" and operation.operation_type in ("renew_user", "renew_purchase")
    ):
        raise HTTPException(409, "payment_reservation_operation_state")
    if row.payer_kind == "customer_wallet":
        entry = db.get(models.LedgerEntry, sale_ledger_entry_id) if sale_ledger_entry_id is not None else None
        if entry is None or entry.kind not in accounting.SALE_KINDS or (
            entry.user_id, entry.amount) != (row.user_id, row.amount):
            raise HTTPException(409, "payment_reservation_ledger_mismatch")
    else:
        entry = accounting.record(db, "admin_credit_spend", row.amount,
            admin_id=row.admin_id, actor_admin_id=operation.actor_id if operation.actor_kind == "admin" else row.admin_id,
            package=package, payment_method="admin_credit", note=f"provisioning:{operation.id}")
        db.flush()
    now = dt.datetime.utcnow()
    result = db.execute(mp.PaymentReservation.__table__.update().where(
        mp.PaymentReservation.id == row.id, mp.PaymentReservation.state == "reserved",
        mp.PaymentReservation.version == row.version,
    ).values(state="captured", captured_at=now, capture_ledger_entry_id=entry.id,
             version=mp.PaymentReservation.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "payment_reservation_conflict")
    db.expire(row)
    return row


def release(db, reservation_id):
    operation, row = _reservation(db, reservation_id)
    if row.state == "released":
        return row
    if row.state == "captured":
        raise HTTPException(409, "payment_reservation_already_captured")
    if operation.state != "compensating":
        raise HTTPException(409, "payment_reservation_operation_state")
    steps = db.execute(select(mp.ProvisioningStep).where(
        mp.ProvisioningStep.operation_id == operation.id)).scalars().all()
    if any(step.state != "removed" or (step.remote_attempted and step.remote_outcome not in (
        "verified_absent", "delete_idempotently_absent")) for step in steps):
        raise HTTPException(409, "payment_reservation_remote_unverified")
    result = db.execute(mp.PaymentReservation.__table__.update().where(
        mp.PaymentReservation.id == row.id, mp.PaymentReservation.state == "reserved",
        mp.PaymentReservation.version == row.version,
    ).values(state="released", released_at=dt.datetime.utcnow(), version=mp.PaymentReservation.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "payment_reservation_conflict")
    if row.payer_kind == "customer_wallet":
        if wallet_service.credit_atomic(db, row.user_id, row.amount,
                                       source_kind=wallet_service.RESERVATION_RELEASE) is None:
            raise HTTPException(409, "payment_reservation_payer_not_found")
    else:
        result = db.execute(models.AdminUser.__table__.update().where(
            models.AdminUser.id == row.admin_id).values(balance=func.coalesce(models.AdminUser.balance, 0) + row.amount))
        if result.rowcount != 1:
            raise HTTPException(409, "payment_reservation_payer_not_found")
    db.expire(row)
    return row
