"""Exact, read-only proration and provenance for unpaid reseller sales.

This module does NOT delete services or move money. Execution must first
stop every connection and obtain verified final usage; cached usage is only
suitable for a preview. Never reconstruct a historical charge from prices.
"""
from __future__ import annotations

import datetime as dt
import json
from fractions import Fraction

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from ..models_reseller_refund import ResellerSaleBasis


def calculate_refund(amount: int, *, used_bytes: int, quota_bytes: int,
                     elapsed_seconds: int, duration_seconds: int) -> dict:
    """Arithmetic mean of finite dimensions; round refund DOWN in tomans.

    An unlimited dimension is omitted, not counted as zero consumption.
    With neither dimension metered there is no defensible proration.
    """
    values = (amount, used_bytes, quota_bytes, elapsed_seconds, duration_seconds)
    if any(type(value) is not int or value < 0 for value in values):
        raise ValueError("refund inputs must be nonnegative integers")
    dimensions = []
    time = quota = None
    if duration_seconds:
        time = min(Fraction(elapsed_seconds, duration_seconds), Fraction(1))
        dimensions.append(time)
    if quota_bytes:
        quota = min(Fraction(used_bytes, quota_bytes), Fraction(1))
        dimensions.append(quota)
    if not dimensions:
        raise ValueError("unmetered service has no proration basis")
    consumed = sum(dimensions, Fraction(0)) / len(dimensions)
    remainder = amount * (1 - consumed)
    refund = remainder.numerator // remainder.denominator
    return {
        "charged_amount": amount,
        "refund_amount": refund,
        "consumed_amount": amount - refund,
        "time_percent": float(time * 100) if time is not None else None,
        "quota_percent": float(quota * 100) if quota is not None else None,
        "consumed_percent": float(consumed * 100),
    }


def bind_sale(db: Session, purchase: models.Purchase, debit: models.LedgerEntry | None,
              package: models.Package, *, sale_entry=None, purchase_count_delta=0) -> None:
    """Bind a SINGLE new service to its actual debit, in the sale commit.

    No debit (superadmin / usage billing / zero price) means no cash refund.
    Bulk charges need explicit allocations and must not pass this function.
    """
    if debit is None:
        return
    if debit.kind != "admin_credit_spend" or not debit.admin_id or debit.amount <= 0:
        raise ValueError("sale basis requires a positive reseller debit")
    db.add(ResellerSaleBasis(
        purchase_id=purchase.id, debit_entry_id=debit.id,
        sale_entry_id=sale_entry.id if sale_entry else None, purchase_count_delta=purchase_count_delta,
        charged_admin_id=debit.admin_id, charged_amount=debit.amount,
        quota_bytes=int(purchase.quota_bytes or 0),
        duration_seconds=int(package.duration_days or 0) * 86400,
        expire_at=purchase.expire_at,
        baseline_used_bytes=int(purchase.used_bytes or 0),
        connection_baselines=json.dumps({str(c.id): int(c.total_bytes or 0)
                                        for c in purchase.connections}, sort_keys=True),
    ))


def preview(db: Session, purchase: models.Purchase, *, now: dt.datetime | None = None) -> dict:
    basis = db.query(ResellerSaleBasis).filter_by(purchase_id=purchase.id).one_or_none()
    if basis is None:
        raise HTTPException(409, "reseller_charge_unproven")
    # A renewal or manual entitlement edit invalidates this one-sale basis.
    # It requires a separate charge allocation, not the original price.
    if (purchase.reserved_created_at is not None
            or int(purchase.quota_bytes or 0) != basis.quota_bytes
            or purchase.expire_at != basis.expire_at):
        raise HTTPException(409, "reseller_entitlement_changed")
    if not db.get(models.AdminUser, basis.charged_admin_id):
        raise HTTPException(409, "reseller_refund_account_missing")
    now = now or dt.datetime.utcnow()
    baselines = json.loads(basis.connection_baselines)
    connections = {str(c.id): int(c.total_bytes or 0) for c in purchase.connections}
    if set(connections) != set(baselines) or any(connections[k] < baselines[k] for k in baselines):
        raise HTTPException(409, "reseller_connections_changed")
    lifetime_used = basis.baseline_used_bytes + sum(connections[k] - baselines[k] for k in baselines)
    elapsed = 0
    if basis.expire_at is not None and basis.duration_seconds:
        start = basis.expire_at - dt.timedelta(seconds=basis.duration_seconds)
        elapsed = max(0, int((now - start).total_seconds()))
    try:
        result = calculate_refund(
            basis.charged_amount, used_bytes=max(int(purchase.used_bytes or 0), lifetime_used),
            quota_bytes=basis.quota_bytes, elapsed_seconds=elapsed,
            duration_seconds=basis.duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(409, "reseller_proration_unavailable") from exc
    return {**result, "purchase_id": purchase.id,
            "refund_admin_id": basis.charged_admin_id,
            "estimate_only": True, "execution_available": False}
