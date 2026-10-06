"""Transactional settlement, not a public remote-delete API.

Remote orchestration must fence all writers and verify final consumption
before calling verify_stopped(). No HTTP payload can supply that evidence.
The opt-in WireGuard worker is its only production caller.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import models
from ..models_reseller_refund import ResellerRefundOperation as Operation
from ..models_reseller_refund import ResellerRefundStep as Step, ResellerSaleBasis as Basis
from . import accounting, hierarchy
from .reseller_refund import preview


def _begin(db: Session):
    if db.new or db.dirty or db.deleted:
        raise RuntimeError("settlement requires a clean session")
    if db.bind.dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))


def _locked(db, model, ident):
    return db.execute(select(model).where(model.id == ident).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def _identity(connection):
    # No raw credential is stored in the operation or logged. A recreated
    # row/renamed credential/retargeted node cannot inherit old evidence.
    fields = ("id", "user_id", "purchase_id", "node_id", "type", "wg_peer_name",
              "wg_public_key", "wg_client_address", "ppp_username", "ppp_password",
              "xr_email", "xr_uuid", "xr_flow")
    node_fields = ("type", "mt_host", "mt_port", "mt_api_ssl_port", "mt_use_ssl",
                   "mt_wireguard_interface", "xr_panel_mode", "xr_ssh_host",
                   "xr_ssh_port", "xr_inbound_tag", "xr_config_path",
                   "xr_panel_base_url", "xr_panel_inbound_id", "se_host",
                   "se_port", "se_hub_name")
    value = {key: str(getattr(connection, key, None)) for key in fields}
    value["node"] = {key: str(getattr(connection.node, key, None)) for key in node_fields}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def prepare(db: Session, *, purchase_id: int, actor: models.AdminUser) -> int:
    """Persist the exact set of connections. Retrying returns the same ID.

    Internal only: creating an intent alone does NOT stop a service, and
    is not sufficient to refund it. This deliberately leaves funds alone.
    """
    try:
        _begin(db)
        existing = db.execute(select(Operation).where(Operation.purchase_id == purchase_id)
                              .with_for_update()).scalar_one_or_none()
        purchase = _locked(db, models.Purchase, purchase_id)
        user_id = existing.user_id if existing is not None else (purchase.user_id if purchase else None)
        user = db.get(models.User, user_id) if user_id is not None else None
        owned = hierarchy.owned_admin_ids(db, actor)
        if user is None or (owned is not None and user.owner_admin_id not in owned):
            raise HTTPException(404, "service_not_found")
        if existing is not None:
            db.commit()
            return existing.id
        preview(db, purchase)  # refuses missing source / changed entitlement
        basis = db.query(Basis).filter_by(purchase_id=purchase_id).one()
        op = Operation(basis_id=basis.id, purchase_id=purchase.id, user_id=user.id,
                       requested_by=actor.id, baseline_used_bytes=int(purchase.used_bytes or 0))
        db.add(op)
        db.flush()
        for connection in purchase.connections:
            db.add(Step(operation_id=op.id, connection_id=connection.id,
                        identity_digest=_identity(connection),
                        baseline_total_bytes=int(connection.total_bytes or 0)))
        ident = op.id
        db.commit()
        return ident
    except Exception:
        db.rollback()
        raise


def capture_counters(db: Session, *, operation_id: int, connection_id: int,
                     download_bytes: int, upload_bytes: int) -> dict:
    """Save stopped raw counters BEFORE deletion, outside any remote call.

    A crash after remote delete can then retry using this durable evidence.
    This does not mark absence verified and never makes an operation payable.
    """
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None or op.state != "pending":
            raise HTTPException(409, "refund_not_pending")
        connection = _locked(db, models.Connection, connection_id)
        step = db.query(Step).filter_by(operation_id=op.id, connection_id=connection_id).one_or_none()
        if step is None or connection is None or _identity(connection) != step.identity_digest:
            raise HTTPException(409, "refund_resource_changed")
        if any(type(v) is not int or v < 0 for v in (download_bytes, upload_bytes)):
            raise HTTPException(409, "refund_usage_unverified")
        if step.captured_at is not None:
            if (step.final_download_counter, step.final_upload_counter) != (download_bytes, upload_bytes):
                raise HTTPException(409, "refund_usage_changed")
        else:
            # Unlike ordinary best-effort polling, a refund may not guess
            # at traffic lost during a counter reset/reboot.
            previous = (int(connection.last_rx_bytes or 0), int(connection.last_tx_bytes or 0))
            if download_bytes < previous[0] or upload_bytes < previous[1]:
                raise HTTPException(409, "refund_counter_reset")
            delta = download_bytes - previous[0] + upload_bytes - previous[1]
            step.final_total_bytes = int(connection.total_bytes or 0) + delta
            step.final_download_counter, step.final_upload_counter = download_bytes, upload_bytes
            step.captured_at = datetime.utcnow()
        result = {"download_bytes": step.final_download_counter,
                  "upload_bytes": step.final_upload_counter, "total_bytes": step.final_total_bytes}
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise


def verify_stopped(db: Session, *, operation_id: int, connection_id: int,
                   final_total_bytes: int) -> None:
    """INTERNAL worker hook: already stopped, absence verified, counters final.

    The worker must retain a non-expiring writer fence until settle commits.
    This function cannot establish remote absence itself. It is never routed
    through FastAPI or accepted from a customer/reseller request.
    """
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None:
            raise HTTPException(404, "refund_not_found")
        if op.state == "completed":
            db.commit()
            return
        step = db.execute(select(Step).where(Step.operation_id == op.id,
                          Step.connection_id == connection_id).with_for_update()).scalar_one_or_none()
        if step is None:
            raise HTTPException(409, "refund_connection_unexpected")
        connection = _locked(db, models.Connection, connection_id)
        if connection is None or _identity(connection) != step.identity_digest:
            raise HTTPException(409, "refund_resource_changed")
        if type(final_total_bytes) is not int or final_total_bytes < step.baseline_total_bytes:
            raise HTTPException(409, "refund_usage_unverified")
        if step.captured_at is not None and final_total_bytes != step.final_total_bytes:
            raise HTTPException(409, "refund_usage_changed")
        # Evidence is monotonic; retry must never replace it with an older read.
        step.final_total_bytes = max(final_total_bytes, step.final_total_bytes or 0)
        step.captured_at = step.captured_at or datetime.utcnow()
        step.verified_at = datetime.utcnow()
        db.commit()
    except Exception:
        db.rollback()
        raise


def settle(db: Session, *, operation_id: int) -> dict:
    """Credit, audit result, and DB removal commit or roll back together."""
    try:
        _begin(db)
        op = _locked(db, Operation, operation_id)
        if op is None:
            raise HTTPException(404, "refund_not_found")
        if op.state == "completed":
            result = {"operation_id": op.id, "refund_amount": op.refund_amount,
                      "state": "completed"}
            db.commit()
            return result
        basis = _locked(db, Basis, op.basis_id)
        purchase = _locked(db, models.Purchase, op.purchase_id)
        if basis is None or purchase is None or purchase.user_id != op.user_id:
            raise HTTPException(409, "refund_resource_changed")
        steps = db.query(Step).filter_by(operation_id=op.id).all()
        connections = list(purchase.connections)
        if (not steps or {s.connection_id for s in steps} != {c.id for c in connections}
                or any(s.verified_at is None for s in steps)):
            raise HTTPException(409, "refund_remote_unverified")
        if any(_identity(c) != next(s.identity_digest for s in steps if s.connection_id == c.id)
               for c in connections):
            raise HTTPException(409, "refund_resource_changed")
        if any(int(c.total_bytes or 0) > next(s.final_total_bytes for s in steps
                                             if s.connection_id == c.id) for c in connections):
            raise HTTPException(409, "refund_usage_changed")
        if db.query(models.RadiusActiveSession).filter(
                models.RadiusActiveSession.connection_id.in_([c.id for c in connections])).first():
            raise HTTPException(409, "refund_sessions_active")
        user = db.get(models.User, op.user_id)
        if user is None:
            raise HTTPException(409, "refund_resource_changed")
        # Counter resets are not supported by this single-sale allocation.
        if int(purchase.used_bytes or 0) < op.baseline_used_bytes:
            raise HTTPException(409, "refund_usage_changed")
        final_used = max(int(purchase.used_bytes or 0), op.baseline_used_bytes +
                         sum(s.final_total_bytes - s.baseline_total_bytes for s in steps))
        # Read-only projection with final, NOT cached, usage. The temporary
        # ORM assignment is rolled back on failure, or deleted on success.
        purchase.used_bytes = final_used
        result = preview(db, purchase, now=max(s.captured_at for s in steps))
        target = _locked(db, models.AdminUser, basis.charged_admin_id)
        if target is None:
            raise HTTPException(409, "reseller_refund_account_missing")
        amount = result["refund_amount"]
        db.execute(models.AdminUser.__table__.update().where(models.AdminUser.id == target.id)
                   .values(balance=models.AdminUser.balance + amount))
        entry = accounting.record(db, "admin_credit_refund", amount, user=user,
                                  admin_id=target.id, actor_admin_id=op.requested_by,
                                  purchase_id=purchase.id, payment_method="admin_credit",
                                  note=f"Unpaid service cancellation #{op.id}; debit #{basis.debit_entry_id}")
        db.flush()
        op.state, op.refund_amount, op.refund_entry_id = "completed", amount, entry.id
        op.completed_at = datetime.utcnow()
        if basis.sale_entry_id:
            sale = _locked(db, models.LedgerEntry, basis.sale_entry_id)
            if sale is None or sale.voided_at is not None:
                raise HTTPException(409, "reseller_sale_unproven")
            sale.voided_at = op.completed_at
        if basis.purchase_count_delta:
            counted = db.execute(models.User.__table__.update().where(
                models.User.id == user.id,
                models.User.purchase_count >= basis.purchase_count_delta)
                       .values(purchase_count=models.User.purchase_count - basis.purchase_count_delta))
            if counted.rowcount != 1:
                raise HTTPException(409, "reseller_purchase_count_unproven")
        # Preserve usage/audit history without dangling foreign keys.
        ids = [c.id for c in connections]
        db.query(models.UsageLog).filter(models.UsageLog.connection_id.in_(ids)).update(
            {models.UsageLog.connection_id: None}, synchronize_session=False)
        db.query(models.RadiusLimitEventLog).filter(models.RadiusLimitEventLog.connection_id.in_(ids)).update(
            {models.RadiusLimitEventLog.connection_id: None}, synchronize_session=False)
        db.query(models.LedgerEntry).filter(models.LedgerEntry.purchase_id == purchase.id).update(
            {models.LedgerEntry.purchase_id: None}, synchronize_session=False)
        for connection in connections:
            db.delete(connection)
        db.delete(purchase)
        db.commit()
        return {"operation_id": op.id, "refund_amount": amount, "state": "completed"}
    except Exception:
        db.rollback()
        raise
