"""The wallet-lot generation of the customer wallet (Receipt Void P9, sections
9, 11, 12.2 - 12.4, 20.5). Allowed only while wallet_runtime_state.phase is
'enforced'; the legacy generation (wallet_service) only in 'normal'.

Every function here works inside the CALLER's transaction and never commits:
the caller has already made itself the writer (SQLite BEGIN IMMEDIATE /
MariaDB locking read), read the phase with require_enforced() as the first
wallet statement, and - for a mutation - revalidated the customer_identity
lease of the account (section 8.3; the durable provisioning operation holds
it for its own transactions, wallet_ops.standalone() for everything else).

What is implemented:
  credit()   C: a credit source, settled against the identity's open debts
             in (created_at, id) order, the rest one active lot;
  hold()     D: a sale debit 'held' with FIFO allocations over the active
             lots (insufficient -> 400, nothing kept); a negative manual
             adjustment is the same, directly 'captured';
  capture()  held -> captured with the sale's LedgerEntry, conditional;
  release()  held -> released, every allocation reversed (R1);
  refund()   captured -> reversed, the same mechanics (R1).
The cache User.balance is checked against the lots before, and written
after, every mutation (20.5): a mismatch is wallet_cache_mismatch, never a
silent repair.

Not here: R2 and V (a refund over a voided lot, the void itself) belong to
the receipt void (P11); a voided lot is refused fail-closed until then.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_receipt_void as rv
from . import receipt_void_schema

ENFORCED = "enforced"
ORIGINS = ("receipt_topup", "loyalty_reward", "referral_reward", "manual_adjustment")


def _now():
    return dt.datetime.utcnow()


def _for_update(db, query, read=False):
    """Row locks on MariaDB; SQLite is already the single writer."""
    if db.get_bind().dialect.name == "sqlite":
        return query
    return query.with_for_update(read=read)


def read_phase(db) -> tuple[str, int]:
    """Current locking read of (phase, epoch) - the first statement of every
    wallet writer transaction (9.2). Held to the commit on MariaDB."""
    receipt_void_schema.assert_ready()
    table = rv.wallet_runtime_state
    row = db.execute(_for_update(db, select(table.c.phase, table.c.epoch).where(
        table.c.id == receipt_void_schema.SINGLETON_ID), read=True)).one_or_none()
    if row is None:
        raise HTTPException(503, "wallet_phase_unavailable")
    return row.phase, int(row.epoch)


def require_enforced(db, *, epoch: int | None = None) -> int:
    """The lot writer runs only in 'enforced'; a multi-transaction operation
    also only in the epoch it started in."""
    phase, current = read_phase(db)
    if phase != ENFORCED:
        raise HTTPException(503, "wallet_lot_writer_unavailable")
    if epoch is not None and epoch != current:
        raise HTTPException(409, "wallet_epoch_changed")
    return current


# ------------------------------------------------------------------ accounts and cache
def account_of_user(db, user_id: int) -> dict:
    """The live account of a customer, or 503 (every User has one after P9)."""
    row = db.execute(select(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id == user_id, rv.wallet_accounts.c.tombstoned_at.is_(None))).mappings().first()
    if row is None:
        raise HTTPException(503, "wallet_account_not_ready")
    return dict(row)


def account(db, account_id: int) -> dict:
    row = db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.id == account_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "wallet_account_not_found")
    return dict(row)


def available(db, account_id: int) -> int:
    lots = rv.wallet_lots
    return int(db.execute(select(func.coalesce(func.sum(lots.c.available_amount), 0)).where(
        lots.c.wallet_account_id == account_id, lots.c.state != "voided")).scalar_one())


def _cached(db, user_id):
    users = models.User.__table__
    return db.execute(_for_update(db, select(users.c.balance).where(users.c.id == user_id))).one_or_none()


def check_cache(db, wallet_account: dict) -> None:
    """Before a mutation: User.balance must equal the lots (I-CACHE)."""
    if wallet_account["user_id"] is None:
        return
    row = _cached(db, wallet_account["user_id"])
    if row is None or int(row.balance or 0) != available(db, wallet_account["id"]):
        raise HTTPException(500, "wallet_cache_mismatch")


def write_cache(db, wallet_account: dict) -> int:
    """After a mutation, in the same transaction: User.balance = Σ available."""
    value = available(db, wallet_account["id"])
    if wallet_account["user_id"] is not None:
        users = models.User.__table__
        db.execute(users.update().where(users.c.id == wallet_account["user_id"]).values(balance=value))
        for instance in list(db.identity_map.values()):
            if isinstance(instance, models.User) and instance.id == wallet_account["user_id"]:
                db.expire(instance, ["balance"])
    return value


def fence_identity(db, identity_id: int) -> None:
    """The customer_identity lease row, locked by this transaction (8.3).

    A single-transaction lot mutation outside a durable operation (a top-up,
    a reward, a manual adjustment) holds this row lock to its commit, so no
    executor can acquire the identity's lease in between. It does not wait
    for, or take over, a lease held by a durable operation: those operations
    revalidate their own lease in each of their transactions, and what this
    one does (one credit, or one complete debit) leaves every row they read
    consistent - the lot CHECKs and versions are the second layer."""
    from sqlalchemy.dialects.mysql import insert as mysql_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    table, key = rv.resource_locks, f"customer_identity:{int(identity_id)}"
    locked = _for_update(db, select(table.c.resource_key).where(table.c.resource_key == key))
    if db.execute(locked).first() is not None:
        return                  # the usual case: lock the existing row directly (no S-then-X upgrade)
    if db.get_bind().dialect.name == "sqlite":
        db.execute(sqlite_insert(table).values(resource_key=key).on_conflict_do_nothing(
            index_elements=[table.c.resource_key]))
    else:
        db.execute(mysql_insert(table).values(resource_key=key).prefix_with("IGNORE"))
    db.execute(locked).one()


# ------------------------------------------------------------------ operations
def request_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def operation(db, operation_type: str, business_key: str, *, payload: dict, tenant_scope_key: str,
              epoch: int, actor_kind: str = "system", actor_id=None, reason=None) -> tuple[int, bool]:
    """The wallet_operations row of one mutation: (id, replayed). The same
    key with the same payload is the same operation; another payload 409."""
    table = rv.wallet_operations
    digest = request_hash(payload)
    existing = db.execute(_for_update(db, select(table.c.id, table.c.request_hash, table.c.state).where(
        table.c.operation_type == operation_type, table.c.business_key == business_key))).one_or_none()
    if existing is not None:
        if existing.request_hash != digest:
            raise HTTPException(409, "wallet_request_changed")
        return int(existing.id), True
    now = _now()
    result = db.execute(table.insert().values(
        operation_type=operation_type, business_key=business_key[:160], request_hash=digest,
        tenant_scope_key=tenant_scope_key, actor_kind=actor_kind, actor_id=actor_id, reason=reason,
        state="running", epoch_at_start=epoch, result_snapshot="{}", attempts=1, created_at=now, started_at=now,
        version=0))
    return int(result.inserted_primary_key[0]), False


def complete(db, operation_id: int, snapshot: dict) -> None:
    table = rv.wallet_operations
    db.execute(table.update().where(table.c.id == operation_id).values(
        state="completed", completed_at=_now(), result_snapshot=json.dumps(snapshot, sort_keys=True, default=str),
        version=table.c.version + 1))


def tenant_of(db, wallet_account: dict) -> str:
    return db.execute(select(rv.customer_identities.c.tenant_scope_key).where(
        rv.customer_identities.c.id == wallet_account["customer_identity_id"])).scalar_one()


# ------------------------------------------------------------------ C: credit apply
def _remaining_debt_expr():
    debt = rv.wallet_debts
    return debt.c.amount - debt.c.adjusted_amount - debt.c.settled_amount


def _debt_state(amount, adjusted, settled):
    obligation = amount - adjusted
    if settled == obligation:
        return "settled"
    return "open" if settled == 0 else "partially_settled"


def _distribute(db, source: dict, wallet_account: dict, operation_id: int, *, exclude_debt=None,
                effect_prefix: str = "") -> None:
    """12.2 distribute(): open debts of the identity first, the rest a lot."""
    debt, apps, sources = rv.wallet_debts, rv.wallet_credit_applications, rv.wallet_credit_sources
    unapplied = int(source["unapplied_amount"])
    query = select(debt).where(debt.c.customer_identity_id == wallet_account["customer_identity_id"],
                               debt.c.state.in_(("open", "partially_settled")))
    if exclude_debt is not None:
        query = query.where(debt.c.id != exclude_debt)
    for row in db.execute(_for_update(db, query.order_by(debt.c.created_at, debt.c.id))).mappings().all():
        if unapplied == 0:
            break
        remaining = int(row["amount"]) - int(row["adjusted_amount"]) - int(row["settled_amount"])
        take = min(unapplied, remaining)
        if take <= 0:
            continue
        db.execute(apps.insert().values(
            wallet_credit_source_id=source["id"], effect_key=f"{effect_prefix}settle:{row['id']}"[:160],
            application_kind="debt_settlement", amount=take, reversed_amount=0, wallet_debt_id=row["id"],
            wallet_operation_id=operation_id, created_at=_now(), version=0))
        settled = int(row["settled_amount"]) + take
        db.execute(debt.update().where(debt.c.id == row["id"], debt.c.version == row["version"]).values(
            settled_amount=settled, state=_debt_state(int(row["amount"]), int(row["adjusted_amount"]), settled),
            version=debt.c.version + 1))
        unapplied -= take
    if unapplied > 0:
        kind = source["origin_kind"] if not effect_prefix and unapplied == source["gross_amount"] else (
            "settlement_reallocation" if effect_prefix else "debt_settlement_remainder")
        lot = db.execute(rv.wallet_lots.insert().values(
            wallet_account_id=source["wallet_account_id"], source_kind=kind,
            source_key=f"{effect_prefix}{source['origin_kind']}:{source['source_key']}"[:160],
            source_credit_source_id=source["id"], original_amount=unapplied, available_amount=unapplied,
            consumed_amount=0, state="active", created_at=_now(), version=0))
        db.execute(apps.insert().values(
            wallet_credit_source_id=source["id"], effect_key=f"{effect_prefix}lot" if effect_prefix else "lot:primary",
            application_kind="lot", amount=unapplied, reversed_amount=0, wallet_lot_id=lot.inserted_primary_key[0],
            wallet_operation_id=operation_id, created_at=_now(), version=0))
    db.execute(sources.update().where(sources.c.id == source["id"]).values(
        unapplied_amount=0, state="applied", version=sources.c.version + 1))


def credit(db, wallet_account: dict, *, origin_kind: str, source_key: str, amount: int, operation_id: int,
           causation_approval_uuid=None, actor_admin_id=None, actor_username=None, reason=None) -> int:
    """C (12.2). Idempotent on (account, origin, source_key): the same credit
    again returns its source; another amount under the same key is 409."""
    if origin_kind not in ORIGINS or type(amount) is not int or amount <= 0:
        raise HTTPException(422, "wallet_credit_invalid")
    sources = rv.wallet_credit_sources
    existing = db.execute(select(sources).where(
        sources.c.wallet_account_id == wallet_account["id"], sources.c.origin_kind == origin_kind,
        sources.c.source_key == source_key)).mappings().first()
    if existing is not None:
        if int(existing["gross_amount"]) != amount:
            raise HTTPException(409, "wallet_credit_changed")
        return int(existing["id"])
    if wallet_account["tombstoned_at"] is not None:
        raise HTTPException(409, "wallet_account_tombstoned")
    check_cache(db, wallet_account)
    result = db.execute(sources.insert().values(
        wallet_account_id=wallet_account["id"], origin_kind=origin_kind, source_key=source_key[:160],
        causation_approval_uuid=causation_approval_uuid, actor_admin_id=actor_admin_id,
        actor_username_snapshot=actor_username, reason=reason, gross_amount=amount, unapplied_amount=amount,
        state="applying", wallet_operation_id=operation_id, created_at=_now(), version=0))
    source = dict(db.execute(select(sources).where(sources.c.id == result.inserted_primary_key[0])).mappings().one())
    _distribute(db, source, wallet_account, operation_id)
    write_cache(db, wallet_account)
    return int(source["id"])


# ------------------------------------------------------------------ D: hold / capture / release
def _debit(db, hold_ref: str):
    debits = rv.wallet_debit_events
    return db.execute(_for_update(db, select(debits).where(debits.c.hold_ref == hold_ref))).mappings().first()


def hold(db, wallet_account: dict, *, amount: int, hold_ref: str, operation_id: int, provisioning_ref=None,
         kind: str = "sale", actor_admin_id=None, reason=None) -> int:
    """D hold (12.3): FIFO over the account's active lots in one statement
    set; insufficient -> 400 and the caller rolls back. A manual adjustment
    is captured at once. Idempotent on hold_ref (same account and amount)."""
    if type(amount) is not int or amount <= 0 or kind not in ("sale", "manual_adjustment"):
        raise HTTPException(422, "wallet_debit_invalid")
    existing = _debit(db, hold_ref)
    if existing is not None:
        if (existing["wallet_account_id"], int(existing["amount"]), existing["kind"]) != (
                wallet_account["id"], amount, kind):
            raise HTTPException(409, "wallet_debit_changed")
        return int(existing["id"])
    if wallet_account["tombstoned_at"] is not None:
        raise HTTPException(409, "wallet_account_tombstoned")
    check_cache(db, wallet_account)
    lots, allocations, debits = rv.wallet_lots, rv.wallet_allocations, rv.wallet_debit_events
    rows = db.execute(_for_update(db, select(lots).where(
        lots.c.wallet_account_id == wallet_account["id"], lots.c.state == "active").order_by(
            lots.c.created_at, lots.c.id))).mappings().all()
    if sum(int(row["available_amount"]) for row in rows) < amount:
        raise HTTPException(400, "موجودی کیف پول کافی نیست")
    now = _now()
    captured = kind == "manual_adjustment"
    debit = db.execute(debits.insert().values(
        wallet_account_id=wallet_account["id"], kind=kind, amount=amount, hold_ref=hold_ref,
        provisioning_ref=provisioning_ref, actor_admin_id=actor_admin_id, reason=reason,
        state="captured" if captured else "held", held_at=now, captured_at=now if captured else None,
        wallet_operation_id=operation_id, created_at=now, version=0)).inserted_primary_key[0]
    remaining = amount
    for row in rows:
        if remaining == 0:
            break
        take = min(int(row["available_amount"]), remaining)
        db.execute(allocations.insert().values(wallet_debit_event_id=debit, wallet_lot_id=row["id"],
                                               allocated_amount=take, created_at=now))
        left = int(row["available_amount"]) - take
        updated = db.execute(lots.update().where(lots.c.id == row["id"], lots.c.version == row["version"]).values(
            available_amount=left, consumed_amount=lots.c.consumed_amount + take,
            state="active" if left > 0 else "exhausted", version=lots.c.version + 1))
        if updated.rowcount != 1:
            raise HTTPException(409, "wallet_lot_changed")
        remaining -= take
    write_cache(db, wallet_account)
    return int(debit)


def capture(db, hold_ref: str, ledger_entry_id: int) -> int:
    """held -> captured, in the transaction that books the sale."""
    debits = rv.wallet_debit_events
    row = _debit(db, hold_ref)
    if row is None:
        raise HTTPException(409, "wallet_debit_not_found")
    if row["state"] == "captured" and row["ledger_entry_id"] == ledger_entry_id:
        return int(row["id"])
    entry = db.get(models.LedgerEntry, ledger_entry_id)
    if entry is None or int(entry.amount) != int(row["amount"]):
        raise HTTPException(409, "wallet_capture_ledger_mismatch")
    result = db.execute(debits.update().where(debits.c.hold_ref == hold_ref, debits.c.state == "held").values(
        state="captured", ledger_entry_id=ledger_entry_id, captured_at=_now(), version=debits.c.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "wallet_debit_not_held")
    return int(row["id"])


def _reverse_allocations(db, debit: dict, operation_id: int) -> None:
    """R1 (12.4) for every allocation of one debit."""
    lots, allocations, reversals = rv.wallet_lots, rv.wallet_allocations, rv.wallet_allocation_reversals
    for allocation in db.execute(select(allocations).where(
            allocations.c.wallet_debit_event_id == debit["id"]).order_by(allocations.c.id)).mappings().all():
        lot = db.execute(_for_update(db, select(lots).where(lots.c.id == allocation["wallet_lot_id"]))).mappings().one()
        if lot["state"] == "voided":
            # R2: the money came from a voided (scam) lot; that is the receipt
            # void's own algorithm (P11), not implemented - never guessed.
            raise HTTPException(409, "wallet_refund_voided_lot_unsupported")
        amount = int(allocation["allocated_amount"])
        db.execute(reversals.insert().values(wallet_allocation_id=allocation["id"], reversed_amount=amount,
                                             lot_was_voided=False, wallet_operation_id=operation_id,
                                             created_at=_now()))
        updated = db.execute(lots.update().where(lots.c.id == lot["id"], lots.c.version == lot["version"]).values(
            available_amount=lots.c.available_amount + amount, consumed_amount=lots.c.consumed_amount - amount,
            state="active", version=lots.c.version + 1))
        if updated.rowcount != 1:
            raise HTTPException(409, "wallet_lot_changed")


def release(db, hold_ref: str, operation_id: int) -> bool:
    """held -> released (the sale did not happen). False: nothing to do
    (already released); a captured debit is never released here."""
    row = _debit(db, hold_ref)
    if row is None:
        raise HTTPException(409, "wallet_debit_not_found")
    if row["state"] == "released":
        return False
    if row["state"] != "held":
        raise HTTPException(409, "wallet_debit_not_held")
    wallet_account = account(db, row["wallet_account_id"])
    check_cache(db, wallet_account)
    _reverse_allocations(db, row, operation_id)
    debits = rv.wallet_debit_events
    result = db.execute(debits.update().where(debits.c.id == row["id"], debits.c.state == "held").values(
        state="released", released_at=_now(), version=debits.c.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "wallet_debit_not_held")
    write_cache(db, wallet_account)
    return True


def refund(db, debit_id: int, operation_id: int) -> bool:
    """captured -> reversed (R1). False: already reversed."""
    debits = rv.wallet_debit_events
    row = db.execute(_for_update(db, select(debits).where(debits.c.id == debit_id))).mappings().first()
    if row is None:
        raise HTTPException(404, "wallet_debit_not_found")
    if row["state"] == "reversed":
        return False
    if row["state"] != "captured":
        raise HTTPException(409, "wallet_debit_not_captured")
    wallet_account = account(db, row["wallet_account_id"])
    check_cache(db, wallet_account)
    _reverse_allocations(db, row, operation_id)
    result = db.execute(debits.update().where(debits.c.id == row["id"], debits.c.state == "captured").values(
        state="reversed", reversed_at=_now(), version=debits.c.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "wallet_debit_not_captured")
    write_cache(db, wallet_account)
    return True


def credit_evidence(db, source_id: int, user_id: int):
    """The effect evidence of one applied credit source: its gross amount and
    how it was applied (debt settlement / lot) - not the balance growth,
    which is smaller when part of it settled open debt."""
    from .receipt_approval_effects import CreditSourceEvidence
    sources, apps = rv.wallet_credit_sources, rv.wallet_credit_applications
    gross = int(db.execute(select(sources.c.gross_amount).where(sources.c.id == source_id)).scalar_one())
    applied = dict(db.execute(select(apps.c.application_kind, func.coalesce(func.sum(apps.c.amount), 0)).where(
        apps.c.wallet_credit_source_id == source_id).group_by(apps.c.application_kind)).all())
    return CreditSourceEvidence(int(source_id), int(user_id), gross, int(applied.get("debt_settlement", 0)),
                                int(applied.get("lot", 0)))


def open_debt(db, identity_id: int) -> int:
    debt = rv.wallet_debts
    return int(db.execute(select(func.coalesce(func.sum(_remaining_debt_expr()), 0)).where(
        debt.c.customer_identity_id == identity_id, debt.c.state.in_(("open", "partially_settled")))).scalar_one())
