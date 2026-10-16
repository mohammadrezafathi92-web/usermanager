"""The receipt void's three phase gates (Receipt Void 21): preview (P10),
execute (P11: accepting NEW voids) and the worker (continuing voids already
running, and their cleanup). All off by default; a superadmin changes them
with the password (routers/receipt_void.py).

  - none of them can be switched on before the joint activation (P9a):
    only an approval registered 'required' after it is voidable at all;
  - execute needs preview, the worker, and the unique index on
    ledger_entries.reversal_of_id verified by the inspector (21, RV-283);
  - switching execute off stops new voids only - running ones go on;
  - switching the worker off is refused while execute is on, or while a
    void is running or in cleanup_required (it would strand it).
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import func, inspect, select

from .. import models, models_receipt_void as rv
from . import receipt_void_schema

FLAGS = {"preview": "receipt_void_preview_enabled", "execute": "receipt_void_execute_enabled",
         "worker": "receipt_void_worker_enabled"}


def _settings(db, lock=False, shared=False):
    query = select(models.PanelSettings).where(models.PanelSettings.id == 1)
    if (lock or shared) and db.get_bind().dialect.name != "sqlite":
        # SQLite: the caller's BEGIN IMMEDIATE is the writer lock already.
        query = query.with_for_update(read=shared and not lock)
    row = db.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:
        row = models.PanelSettings(id=1)
        db.add(row)
        db.flush()
    return row


def state(db) -> dict:
    row = _settings(db)
    return {name: bool(getattr(row, column)) for name, column in FLAGS.items()}


def enabled(db, name: str) -> bool:
    return bool(getattr(_settings(db), FLAGS[name]))


def require(db, name: str) -> None:
    if not receipt_void_schema.is_ready() or not enabled(db, name):
        raise HTTPException(404, "receipt_void_disabled")


def lock_accepting(db) -> None:
    """In a void's claim transaction (opened with begin_business): the
    settings row under a SHARED lock, so set_flag (exclusive) cannot change a
    flag between this check and the claim's commit - a claim is accepted
    only while execute AND the worker are on, and the worker cannot be
    switched off by a transaction that did not yet see the new operation.
    404 like every other disabled route."""
    row = _settings(db, shared=True)
    if not (bool(row.receipt_void_execute_enabled) and bool(row.receipt_void_worker_enabled)):
        raise HTTPException(404, "receipt_void_disabled")


def _activated(db) -> bool:
    row = db.execute(select(rv.receipt_approval_runtime_state)).mappings().first()
    return bool(row) and row["registration_mode"] == "required" and row["loyalty_timing_mode"] == "payment_event"


def _reversal_index_ready(db) -> bool:
    indexes = inspect(db.get_bind()).get_indexes("ledger_entries")
    return any(index.get("name") == "uq_ledger_entries_reversal_of_id" and index.get("unique")
               and index.get("column_names") == ["reversal_of_id"] for index in indexes)


def set_flag(db, name: str, value: bool) -> dict:
    """Commits. 409 with the reason on a refused change."""
    if name not in FLAGS:
        raise HTTPException(422, "receipt_void_flag_invalid")
    receipt_void_schema.assert_ready()
    from . import resource_leases
    if db.in_transaction():
        db.rollback()
    resource_leases.begin_business(db)          # SQLite: BEGIN IMMEDIATE; serializes with a claim
    row = _settings(db, lock=True)
    current = {name: bool(getattr(row, column)) for name, column in FLAGS.items()}
    if value and not _activated(db):
        raise HTTPException(409, "receipt_void_requires_joint_activation")
    if value and name == "execute" and not current["preview"]:
        raise HTTPException(409, "receipt_void_execute_requires_preview")
    if value and name == "execute" and not _reversal_index_ready(db):
        raise HTTPException(409, "ledger_reversal_index_missing")
    if value and name == "execute" and not current["worker"]:
        # A void accepted with no worker would wait in 'running' forever.
        raise HTTPException(409, "receipt_void_execute_requires_worker")
    if not value and name == "worker" and current["execute"]:
        raise HTTPException(409, "receipt_void_worker_needed_by_execute")
    if not value and name == "worker":
        active = db.execute(select(func.count()).select_from(rv.wallet_operations).where(
            rv.wallet_operations.c.operation_type == "void_receipt",
            rv.wallet_operations.c.state.in_(("running", "cleanup_required", "pending")))).scalar_one()
        if active:
            raise HTTPException(409, "receipt_void_operations_active")
    if not value and name == "preview" and current["execute"]:
        raise HTTPException(409, "receipt_void_preview_needed_by_execute")
    setattr(row, FLAGS[name], bool(value))
    db.commit()
    db.info.pop("resource_lease_business_transaction", None)
    return state(db)
