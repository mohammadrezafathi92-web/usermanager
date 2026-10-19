"""P9a reservation transition (addendum receipt-void-addendum-p9a-
reservations-2026-10-19.md): Purchase reservations pass the joint
activation with an immutable snapshot instead of blocking it.

A Purchase is identified by (id, created_at): SQLite may give a deleted
purchase's id to a new one, and no history may attach to that new one.

Writers of Purchase reservations and how each is ordered against the
activation (which holds receipt_approval_runtime_state EXCLUSIVE and every
reserved purchase row FOR UPDATE until its commit):

  renew_purchase_core (queue)     only in a durable T_final, which holds the
                                  runtime row SHARED from its start (pin_mode)
  absorb / migration (new row)    T_final (pinned) or startup, before serving
  consume (poller / RADIUS)       lock_for_consumption: the purchase row FOR
                                  UPDATE first, then compare the sequence
  cap backfill, package deletion, UPDATE of the row itself (row lock)
  session-cap repair
  reservation resolution API      the runtime row SHARED (reservation_resolution)

So under READ COMMITTED as under REPEATABLE READ no reservation is created,
changed or consumed between the activation's locking read and its commit:
either the writer committed first (the activation's locking read sees its
result) or it waits for the activation's commit.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from sqlalchemy import or_, select

from .. import models, models_reservation_transition as rt
from . import receipt_void_schema

EPOCH = dt.datetime(1970, 1, 1)
RESERVED_FIELDS = ("reserved_quota_bytes", "reserved_duration_days", "reserved_package_id",
                   "reserved_max_concurrent_sessions", "reserved_replace_package_limits", "reserved_created_at")
PRIOR_RIGHTS_NOTE = ("snapshot rows show what was QUEUED on a purchase when payment-time loyalty began; they are not "
                     "a record of every right the customer acquired before it")


def _stamp(value) -> Optional[str]:
    return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value


def _created(purchase_created_at) -> dt.datetime:
    return purchase_created_at or EPOCH


def _same_instant(a, b) -> bool:
    # MariaDB DATETIME keeps seconds; compare at the coarser precision both keep
    a, b = _created(a), _created(b)
    return a.replace(microsecond=0) == b.replace(microsecond=0)


def is_reserved(row) -> bool:
    return bool(row.reserved_quota_bytes or row.reserved_duration_days or row.reserved_package_id is not None)


def purchase_state(purchase) -> dict:
    """The service's limits and its queue - not usage, which the poller moves."""
    state = {"quota_bytes": int(purchase.quota_bytes or 0), "expire_at": _stamp(purchase.expire_at),
             "package_id": purchase.package_id, "max_concurrent_sessions": purchase.max_concurrent_sessions,
             "status": getattr(purchase.status, "value", purchase.status)}
    state.update({name: _stamp(getattr(purchase, name)) for name in RESERVED_FIELDS})
    return state


# ------------------------------------------------------------------ consumption
def lock_for_consumption(db, purchase) -> Optional[int]:
    """The purchase row locked, its reservation still there and not consumed
    by another transaction since this one loaded it -> the sequence to
    consume; otherwise None (nothing is applied). Pending changes are flushed
    first so the locked row is what this transaction will write."""
    db.flush()
    live = db.execute(select(models.Purchase.reservation_activation_seq, models.Purchase.reserved_quota_bytes,
                             models.Purchase.reserved_duration_days, models.Purchase.reserved_package_id)
                      .where(models.Purchase.id == purchase.id).with_for_update()).first()
    if live is None or not is_reserved(live):
        return None
    seq = int(live.reservation_activation_seq or 0)
    if seq != int(purchase.reservation_activation_seq or 0):
        return None
    return seq


def log_consumption(db, purchase, seq_before: int, trigger: str, before: dict) -> None:
    """In the consuming transaction. A legacy installation without the
    Receipt Void schema keeps consuming (no snapshot can exist there)."""
    if not receipt_void_schema.is_ready():
        return
    db.execute(rt.purchase_reservation_activations.insert().values(
        purchase_id=purchase.id, purchase_created_at=_created(purchase.created_at), user_id_snapshot=purchase.user_id,
        seq_before=seq_before, activated_by=trigger if trigger in rt.ACTIVATION_TRIGGERS else "other",
        before_json=json.dumps(before, sort_keys=True, default=str),
        after_json=json.dumps(purchase_state(purchase), sort_keys=True, default=str),
        created_at=dt.datetime.utcnow()))


# ------------------------------------------------------------------ snapshot (inside the activation)
def _row_hash(values: dict) -> str:
    keys = ("generation", "purchase_id", "purchase_created_at", "user_id_snapshot", "owner_admin_id_snapshot",
            "reservation_activation_seq") + RESERVED_FIELDS
    payload = {k: _stamp(values[k]) if isinstance(values[k], dt.datetime) else values[k] for k in keys}
    if payload["purchase_created_at"]:
        payload["purchase_created_at"] = payload["purchase_created_at"][:19]
    if payload["reserved_created_at"]:
        payload["reserved_created_at"] = payload["reserved_created_at"][:19]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def snapshot_purchases(db, generation: int) -> int:
    """Every reserved purchase, locked (in id order) to the caller's commit,
    and one immutable snapshot row each. The caller holds the runtime row
    exclusively; nothing about the reservation itself changes."""
    P = models.Purchase
    rows = db.execute(select(P.id, P.created_at, P.user_id, P.reservation_activation_seq,
                             P.reserved_quota_bytes, P.reserved_duration_days, P.reserved_package_id,
                             P.reserved_max_concurrent_sessions, P.reserved_replace_package_limits,
                             P.reserved_created_at)
                      .where(or_(P.reserved_quota_bytes > 0, P.reserved_duration_days > 0, P.reserved_package_id.isnot(None)))
                      .order_by(P.id).with_for_update()).all()
    owners = dict(db.execute(select(models.User.id, models.User.owner_admin_id).where(
        models.User.id.in_({r.user_id for r in rows}))).all()) if rows else {}
    now = dt.datetime.utcnow()
    for r in rows:
        values = {"generation": generation, "purchase_id": r.id, "purchase_created_at": _created(r.created_at),
                  "user_id_snapshot": r.user_id, "owner_admin_id_snapshot": owners.get(r.user_id),
                  "reservation_activation_seq": int(r.reservation_activation_seq or 0),
                  **{name: getattr(r, name) for name in RESERVED_FIELDS}}
        db.execute(rt.loyalty_cutover_purchase_reservations.insert().values(
            **values, row_hash=_row_hash(values), created_at=now))
    return len(rows)


# ------------------------------------------------------------------ reading the history
def _consumption(db, snapshot) -> Optional[dict]:
    table = rt.purchase_reservation_activations
    for row in db.execute(select(table).where(table.c.purchase_id == snapshot["purchase_id"],
                                              table.c.seq_before == snapshot["reservation_activation_seq"])).mappings():
        if _same_instant(row["purchase_created_at"], snapshot["purchase_created_at"]):
            return dict(row)
    return None


def snapshots_of(db, purchases) -> list[dict]:
    """The snapshots of exactly these live purchases (id AND created_at), each
    with whether - and by which logged consumption - it was consumed."""
    if not receipt_void_schema.is_ready() or not purchases:
        return []
    table = rt.loyalty_cutover_purchase_reservations
    created = {p.id: p.created_at for p in purchases}
    out = []
    for row in db.execute(select(table).where(table.c.purchase_id.in_(created)).order_by(table.c.id)).mappings():
        if not _same_instant(row["purchase_created_at"], created[row["purchase_id"]]):
            continue
        consumed = _consumption(db, row)
        out.append({"snapshot_id": int(row["id"]), "generation": int(row["generation"]),
                    "purchase_id": int(row["purchase_id"]),
                    **{name: _stamp(row[name]) for name in RESERVED_FIELDS},
                    "consumed": consumed is not None,
                    "consumed_by_activation_id": int(consumed["id"]) if consumed else None})
    return out


def check(db) -> list[dict]:
    """Read-only integrity alerts (wallet_reconcile): a snapshot whose content
    no longer matches its hash, or whose live purchase consumed past it with
    no logged consumption. A deleted purchase, or one whose id now belongs to
    a newer purchase, is history - not an alert."""
    problems = []
    table = rt.loyalty_cutover_purchase_reservations
    for row in db.execute(select(table).order_by(table.c.id)).mappings():
        values = dict(row)
        if values["row_hash"] != _row_hash(values):
            problems.append({"code": "cutover_snapshot_changed", "snapshot_id": int(row["id"])})
            continue
        live = db.get(models.Purchase, row["purchase_id"])
        if live is None or not _same_instant(live.created_at, row["purchase_created_at"]):
            continue
        if int(live.reservation_activation_seq or 0) > int(row["reservation_activation_seq"]) and _consumption(db, row) is None:
            problems.append({"code": "cutover_snapshot_consumption_missing", "snapshot_id": int(row["id"]),
                             "purchase_id": int(row["purchase_id"])})
    return problems
