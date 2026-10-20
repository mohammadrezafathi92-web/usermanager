"""P9a reservation transition and per-payment renewal history (addendum
receipt-void-addendum-p9a-reservations-2026-10-19.md).

  * Purchase reservations pass the joint activation with an immutable
    snapshot instead of blocking it;
  * every renewal of a Purchase records its own share (contribution) and
    every queue consumption is logged, both ordered per purchase by
    Purchase.renewal_event_seq under the row lock - so a renewal void can
    take back exactly one payment's right (services/receipt_void_renewal.py).

A Purchase is identified by Purchase.history_key, never by id or time:
SQLite may give a deleted purchase's id to a new one, even in the same
second. A row from before the key existed gets it under the row lock the
first time history is written.

Writers of Purchase reservations and how each is ordered against the
activation (which holds receipt_approval_runtime_state EXCLUSIVE and every
reserved purchase row FOR UPDATE until its commit):

  renew_purchase_core (queue)     only in a durable T_final, which holds the
                                  runtime row SHARED from its start (pin_mode)
  absorb / migration (new row)    T_final (pinned) or startup, before serving
  consume (poller / RADIUS)       lock_for_consumption: the purchase row FOR
                                  UPDATE first, then the live queue re-read
  cap backfill, package deletion, UPDATE of the row itself (row lock)
  session-cap repair
  reservation resolution API      the runtime row SHARED (reservation_resolution)

So under READ COMMITTED as under REPEATABLE READ no reservation is created,
changed or consumed between the activation's locking read and its commit.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from typing import Optional

from sqlalchemy import event, inspect as sa_inspect, or_, select
from sqlalchemy.orm import Session

from .. import models, models_reservation_transition as rt
from . import receipt_void_schema

PENDING = "reservation_history_pending"
RESERVED_FIELDS = ("reserved_quota_bytes", "reserved_duration_days", "reserved_package_id",
                   "reserved_max_concurrent_sessions", "reserved_replace_package_limits", "reserved_created_at")
LIMIT_FIELDS = ("quota_bytes", "expire_at", "package_id", "max_concurrent_sessions")
PRIOR_RIGHTS_NOTE = ("snapshot rows show what was QUEUED on a purchase when payment-time loyalty began; they are not "
                     "a record of every right the customer acquired before it")


def _stamp(value):
    return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value


def is_reserved(row) -> bool:
    return bool(row.reserved_quota_bytes or row.reserved_duration_days or row.reserved_package_id is not None)


def purchase_state(purchase) -> dict:
    """The service's limits and its queue - not usage, which the poller moves."""
    state = {"quota_bytes": int(purchase.quota_bytes or 0), "expire_at": _stamp(purchase.expire_at),
             "package_id": purchase.package_id, "max_concurrent_sessions": purchase.max_concurrent_sessions,
             "status": getattr(purchase.status, "value", purchase.status)}
    state.update({name: _stamp(getattr(purchase, name)) for name in RESERVED_FIELDS})
    return state


def full_state(purchase) -> dict:
    """purchase_state plus the usage at this instant and the sequences."""
    return {**purchase_state(purchase), "used_bytes": int(purchase.used_bytes or 0),
            "reservation_activation_seq": int(purchase.reservation_activation_seq or 0),
            "renewal_event_seq": int(purchase.renewal_event_seq or 0)}


def ensure_key(db, purchase) -> str:
    """The purchase's history key (written with the purchase at the flush);
    the caller holds its row lock."""
    if not purchase.history_key:
        purchase.history_key = str(uuid.uuid4())
    return purchase.history_key


def _sqlite(db) -> bool:
    return db.get_bind().dialect.name == "sqlite"


def _lock_row(db, purchase):
    """The committed row. MariaDB: this transaction's changes flushed, then
    the row locked FOR UPDATE. SQLite: one writer serializes every commit
    and a plain read sees the last commit - no flush here, so SQLite's
    writer is not taken before a caller's node call (the history rows are
    written at the transaction's own flush, see _write_pending)."""
    P = models.Purchase
    query = select(P.reservation_activation_seq, P.renewal_event_seq, P.history_key, P.quota_bytes,
                   P.expire_at, *[getattr(P, name) for name in RESERVED_FIELDS]).where(P.id == purchase.id)
    if _sqlite(db):
        return db.execute(query).first()
    db.flush()
    return db.execute(query.with_for_update()).first()


def _pending(purchase, name) -> bool:
    """This transaction already changed the attribute (keep its own write)."""
    return sa_inspect(purchase).attrs[name].history.has_changes()


def _queue(db, table, values: dict) -> None:
    db.info.setdefault(PENDING, []).append((table, values))


@event.listens_for(Session, "before_flush")
def _write_pending(session, _context, _instances):
    """History rows go to the database with the change they describe, in the
    same flush of the same transaction (never earlier)."""
    rows = session.info.pop(PENDING, None)
    if rows:
        connection = session.connection()
        for table, values in rows:
            connection.execute(table.insert().values(**values))


@event.listens_for(Session, "after_soft_rollback")
def _drop_pending(session, _previous_transaction):
    """A rolled-back change takes its history rows with it."""
    session.info.pop(PENDING, None)


def lock_history(db, purchase) -> None:
    """The purchase row locked for a history write; the ORM object brought
    to the committed history columns (a stale copy never decides)."""
    live = _lock_row(db, purchase)
    if live is None:
        return
    if not _pending(purchase, "renewal_event_seq"):
        purchase.renewal_event_seq = live.renewal_event_seq
    if not _pending(purchase, "reservation_activation_seq"):
        purchase.reservation_activation_seq = live.reservation_activation_seq
    if live.history_key and purchase.history_key != live.history_key:
        purchase.history_key = live.history_key
    ensure_key(db, purchase)


def next_event(purchase) -> int:
    seq = int(purchase.renewal_event_seq or 0) + 1
    purchase.renewal_event_seq = seq
    return seq


# ------------------------------------------------------------------ consumption
def lock_for_consumption(db, purchase) -> Optional[int]:
    """The purchase row locked; the queue and limits re-read from it under
    the lock and put on the ORM object (this transaction's pending usage is
    kept - it was flushed first). None when another transaction consumed it
    since this one loaded the purchase, or when no queue is left."""
    live = _lock_row(db, purchase)
    if live is None:
        return None
    seq = int(live.reservation_activation_seq or 0)
    if seq != int(purchase.reservation_activation_seq or 0):
        return None
    for name in RESERVED_FIELDS + ("quota_bytes", "expire_at"):
        # the committed value, unless this transaction wrote its own
        if not _pending(purchase, name) and getattr(purchase, name) != getattr(live, name):
            setattr(purchase, name, getattr(live, name))
    if not is_reserved(purchase):
        return None
    purchase.renewal_event_seq = live.renewal_event_seq
    if live.history_key:
        purchase.history_key = live.history_key
    ensure_key(db, purchase)
    return seq


def log_consumption(db, purchase, seq_before: int, trigger: str, before: dict, used_before: int,
                    event_seq: int) -> None:
    """In the consuming transaction. A legacy installation without the
    Receipt Void schema keeps consuming (no history exists there)."""
    if not receipt_void_schema.is_ready():
        return
    _queue(db, rt.purchase_reservation_activations, dict(
        purchase_id=purchase.id, purchase_key=purchase.history_key, event_seq=event_seq,
        user_id_snapshot=purchase.user_id, seq_before=seq_before, used_bytes_before=int(used_before or 0),
        activated_by=trigger if trigger in rt.ACTIVATION_TRIGGERS else "other",
        before_json=json.dumps(before, sort_keys=True, default=str),
        after_json=json.dumps(purchase_state(purchase), sort_keys=True, default=str),
        created_at=dt.datetime.utcnow()))


def record_contribution(db, purchase, *, mode: str, before: dict, add_quota_bytes: int, add_days: int,
                        package_id, max_concurrent_sessions, package_limits: bool, batch_seq: Optional[int],
                        ref: Optional[dict] = None) -> None:
    """One payment's share of a purchase's renewal history, in the renewing
    transaction (the caller locked the row with lock_history)."""
    event_seq = next_event(purchase)
    if not receipt_void_schema.is_ready():
        return
    ref = ref or {}
    _queue(db, rt.purchase_renewal_contributions, dict(
        purchase_id=purchase.id, purchase_key=purchase.history_key, event_seq=event_seq, mode=mode,
        batch_seq=batch_seq, approval_uuid=ref.get("approval_uuid"),
        provisioning_operation_id=ref.get("operation_id"), add_quota_bytes=int(add_quota_bytes or 0),
        add_days=int(add_days or 0), package_id=package_id, max_concurrent_sessions=max_concurrent_sessions,
        package_limits=bool(package_limits), before_json=json.dumps(before, sort_keys=True, default=str),
        after_json=json.dumps(full_state(purchase), sort_keys=True, default=str), created_at=dt.datetime.utcnow()))


# ------------------------------------------------------------------ snapshot (inside the activation)
def _row_hash(values: dict) -> str:
    keys = ("generation", "purchase_id", "purchase_key", "user_id_snapshot", "owner_admin_id_snapshot",
            "reservation_activation_seq") + RESERVED_FIELDS
    payload = {k: values[k] for k in keys}
    if isinstance(payload["reserved_created_at"], dt.datetime):
        # DATETIME on MariaDB keeps seconds: hash what both engines store
        payload["reserved_created_at"] = payload["reserved_created_at"].replace(microsecond=0).isoformat(sep=" ")
    if payload["reserved_replace_package_limits"] is not None:
        payload["reserved_replace_package_limits"] = bool(payload["reserved_replace_package_limits"])
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def snapshot_purchases(db, generation: int) -> int:
    """Every reserved purchase, locked (in id order) to the caller's commit,
    and one immutable snapshot row each. The caller holds the runtime row
    exclusively; nothing about the reservation itself changes."""
    P = models.Purchase
    purchases = list(db.execute(select(P).where(or_(
        P.reserved_quota_bytes > 0, P.reserved_duration_days > 0, P.reserved_package_id.isnot(None)))
        .order_by(P.id).with_for_update().execution_options(populate_existing=True)).scalars())
    owners = dict(db.execute(select(models.User.id, models.User.owner_admin_id).where(
        models.User.id.in_({p.user_id for p in purchases}))).all()) if purchases else {}
    now = dt.datetime.utcnow()
    for purchase in purchases:
        ensure_key(db, purchase)
        values = {"generation": generation, "purchase_id": purchase.id, "purchase_key": purchase.history_key,
                  "user_id_snapshot": purchase.user_id, "owner_admin_id_snapshot": owners.get(purchase.user_id),
                  "reservation_activation_seq": int(purchase.reservation_activation_seq or 0),
                  **{name: getattr(purchase, name) for name in RESERVED_FIELDS}}
        db.execute(rt.loyalty_cutover_purchase_reservations.insert().values(
            **values, row_hash=_row_hash(values), created_at=now))
    return len(purchases)


# ------------------------------------------------------------------ reading the history
def _consumption(db, snapshot) -> Optional[dict]:
    table = rt.purchase_reservation_activations
    row = db.execute(select(table).where(table.c.purchase_key == snapshot["purchase_key"],
                                         table.c.seq_before == snapshot["reservation_activation_seq"])).mappings().first()
    return dict(row) if row else None


def snapshots_of(db, purchases) -> list[dict]:
    """The snapshots of exactly these live purchases (by history key), each
    with whether - and by which logged consumption - it was consumed."""
    keys = {p.history_key: p.id for p in purchases if p.history_key}
    if not receipt_void_schema.is_ready() or not keys:
        return []
    table = rt.loyalty_cutover_purchase_reservations
    out = []
    for row in db.execute(select(table).where(table.c.purchase_key.in_(keys)).order_by(table.c.id)).mappings():
        consumed = _consumption(db, row)
        out.append({"snapshot_id": int(row["id"]), "generation": int(row["generation"]),
                    "purchase_id": keys[row["purchase_key"]],
                    **{name: _stamp(row[name]) for name in RESERVED_FIELDS},
                    "consumed": consumed is not None,
                    "consumed_by_activation_id": int(consumed["id"]) if consumed else None})
    return out


def check(db) -> list[dict]:
    """Read-only integrity alerts (wallet_reconcile): a snapshot whose content
    no longer matches its hash, or whose live purchase consumed past it with
    no logged consumption. A deleted purchase, or a newer purchase with its
    id, is history - not an alert."""
    problems = []
    table = rt.loyalty_cutover_purchase_reservations
    for row in db.execute(select(table).order_by(table.c.id)).mappings():
        if row["row_hash"] != _row_hash(dict(row)):
            problems.append({"code": "cutover_snapshot_changed", "snapshot_id": int(row["id"])})
            continue
        live = db.execute(select(models.Purchase).where(models.Purchase.history_key == row["purchase_key"])).scalar_one_or_none()
        if live is None:
            continue
        if int(live.reservation_activation_seq or 0) > int(row["reservation_activation_seq"]) and _consumption(db, row) is None:
            problems.append({"code": "cutover_snapshot_consumption_missing", "snapshot_id": int(row["id"]),
                             "purchase_id": int(row["purchase_id"])})
    return problems
