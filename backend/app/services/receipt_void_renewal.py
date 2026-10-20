"""Void of a renewal receipt: only THAT payment's right on THAT service
(owner decision 2026-10-20; addendum receipt-void-addendum-p9a-reservations-
2026-10-19.md, section 6). The button is for a fake / invalid receipt: no
money moves to the customer, nothing else of the account is touched.

The payment's own share is its purchase_renewal_contributions row; the
service's history is ordered by event_seq (reservation_transition). Cases:

  queued        the renewal is still in the queue (its batch not consumed):
                the queue is rebuilt from the batch's contributions without
                this one, with THE queueing rule (user_ops.queue_reservation);
                usage, limits and the other renewals' meaning are kept;
  current_term  the term this renewal made is the current one (an immediate
                reset, or the consumption of a batch this renewal alone
                filled): the limits from just before that term come back -
                the real previous expiry, never today + duration, so a term
                that had ended stays ended; its usage at that moment comes
                back; what the voided term consumed is recorded apart (never
                usable again, never charged to the previous term), with its
                unused share set aside;
  superseded    a later legitimate payment already started another term: the
                service is not touched; the voided term's real consumption and
                unused share are recorded, or - when the term was shared with
                another payment - its usage as UNKNOWN (never as consumed);
  resource_deleted  the service no longer exists: nothing to take back.

Fail closed, before any mutation, when the history cannot prove the share
(no contribution, a queue whose base or replay does not match the live
queue, a term shared with another payment, limits changed by anything but
this history): RenewalRefused with the reason. Force never bypasses it.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_receipt_void as rv, models_reservation_transition as rt
from . import reservation_transition, user_ops

QUEUE_FIELDS = user_ops.QUEUE_FIELDS
LIMITS = ("quota_bytes", "expire_at", "package_id", "max_concurrent_sessions")
SCOPE_NOTE = ("voids an invalid / fake receipt: only this renewal's right on this service is taken back; no refund, "
              "no wallet credit to the customer, no other service or earlier valid term is touched")


class RenewalRefused(HTTPException):
    def __init__(self, code: str):
        super().__init__(409, code)
        self.code = code


def _parse_dt(value):
    if value is None or isinstance(value, dt.datetime):
        return value
    return dt.datetime.fromisoformat(str(value))


def _limits(state: dict) -> dict:
    return {"quota_bytes": int(state.get("quota_bytes") or 0), "expire_at": _parse_dt(state.get("expire_at")),
            "package_id": state.get("package_id"), "max_concurrent_sessions": state.get("max_concurrent_sessions")}


def _live_limits(purchase) -> dict:
    return {"quota_bytes": int(purchase.quota_bytes or 0), "expire_at": purchase.expire_at,
            "package_id": purchase.package_id, "max_concurrent_sessions": purchase.max_concurrent_sessions}


def _same_limits(a: dict, b: dict) -> bool:
    def norm(x):
        e = x["expire_at"]
        # MariaDB DATETIME keeps seconds: compare at the precision it stores
        return (x["quota_bytes"], e.replace(microsecond=0) if isinstance(e, dt.datetime) else e, x["package_id"],
                x["max_concurrent_sessions"])
    return norm(a) == norm(b)


def _queue_of(state: dict) -> dict:
    return {name: state.get(name) for name in QUEUE_FIELDS}


def _empty(queue: dict) -> bool:
    return not (queue["reserved_quota_bytes"] or queue["reserved_duration_days"] or queue["reserved_package_id"] is not None)


def _rows(db, table, key):
    return [dict(r) for r in db.execute(select(table).where(table.c.purchase_key == key)
                                        .order_by(table.c.event_seq)).mappings()]


def _replay(contributions: list[dict]) -> dict:
    queue = {name: None for name in QUEUE_FIELDS}
    for c in contributions:
        if c["mode"] == "transfer":
            queue = {"reserved_quota_bytes": c["add_quota_bytes"] or None, "reserved_duration_days": c["add_days"] or None,
                     "reserved_package_id": c["package_id"], "reserved_max_concurrent_sessions": c["max_concurrent_sessions"],
                     "reserved_replace_package_limits": None}
        else:
            queue = user_ops.queue_reservation(queue, add_quota_bytes=c["add_quota_bytes"], add_days=c["add_days"],
                                               package_id=c["package_id"],
                                               max_concurrent_sessions=c["max_concurrent_sessions"],
                                               package_limits=bool(c["package_limits"]))
    return queue


def _norm_queue(queue: dict) -> tuple:
    return (int(queue["reserved_quota_bytes"] or 0), int(queue["reserved_duration_days"] or 0),
            queue["reserved_package_id"], queue["reserved_max_concurrent_sessions"],
            bool(queue["reserved_replace_package_limits"]))


def _own_quota_grants(db, approval_uuid: str, purchase_id: int) -> int:
    """Quota this same approval's loyalty reward put on the service (reversed
    by the loyalty step before the renewal step)."""
    rewards = rv.loyalty_reward_events
    total = 0
    # what of it is still on the service: granted minus what a reversal
    # (this void's loyalty step, possibly only the unused part) took back
    for row in db.execute(select(rewards.c.quota_before, rewards.c.quota_after, rewards.c.quota_reversed_bytes).where(
            rewards.c.causation_approval_uuid == approval_uuid,
            rewards.c.quota_target_type == "Purchase", rewards.c.quota_target_id == purchase_id)):
        if row.quota_after is not None and row.quota_before is not None:
            total += int(row.quota_after) - int(row.quota_before) - int(row.quota_reversed_bytes or 0)
    return total


def _consumption(term_quota: int, used: Optional[int], shared: bool) -> dict:
    if used is None:
        return {"consumed_bytes": None, "set_aside_bytes": None, "unknown_bytes": None}
    if shared:
        return {"consumed_bytes": None, "set_aside_bytes": None, "unknown_bytes": int(used)}
    if term_quota > 0:
        return {"consumed_bytes": min(int(used), term_quota), "set_aside_bytes": max(term_quota - int(used), 0),
                "unknown_bytes": None}
    return {"consumed_bytes": int(used), "set_aside_bytes": None, "unknown_bytes": None}   # an unlimited term


def plan(db, approval: dict, *, locked: bool = False) -> dict:
    """The exact reversal of this renewal, or RenewalRefused. `bound` is what
    the confirmation binds (no live usage); `consumption` is informational
    until the step computes it again under its locks."""
    approval_uuid = approval["approval_uuid"]
    table = rt.purchase_renewal_contributions
    mine = [dict(r) for r in db.execute(select(table).where(table.c.approval_uuid == approval_uuid)).mappings()]
    if len(mine) != 1:
        raise RenewalRefused("renewal_history_unavailable")
    c = mine[0]
    query = select(models.Purchase).where(models.Purchase.history_key == c["purchase_key"])
    if locked:
        query = query.with_for_update()
    purchase = db.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    base = {"contribution_id": int(c["id"]), "purchase_key": c["purchase_key"], "purchase_id": int(c["purchase_id"]),
            "share": {"add_quota_bytes": int(c["add_quota_bytes"]), "add_days": int(c["add_days"]),
                      "package_id": c["package_id"], "max_concurrent_sessions": c["max_concurrent_sessions"],
                      "package_limits": bool(c["package_limits"]), "mode": c["mode"]}}
    if purchase is None:
        return {"case": "resource_deleted", "bound": {**base, "case": "resource_deleted"}, "before": None,
                "after": None, "consumption": _consumption(0, None, False), "purchase": None}
    contributions = _rows(db, table, c["purchase_key"])
    activations = _rows(db, rt.purchase_reservation_activations, c["purchase_key"])
    # Earlier completed renewal voids of this service are part of its history:
    # a queued share they removed never reached a term, and a term start they
    # undid no longer ends the term before it. The usage they recorded stays.
    earlier = [dict(r) for r in db.execute(select(rt.receipt_void_renewal_reversals).where(
        rt.receipt_void_renewal_reversals.c.purchase_key == c["purchase_key"],
        rt.receipt_void_renewal_reversals.c.approval_uuid != approval_uuid)).mappings()]
    removed = {r["approval_uuid"] for r in earlier if r["reversal_case"] == "queued"}
    undone = {r["term_start"] for r in earlier if r["reversal_case"] == "current_term" and r["term_start"]}
    effective = [x for x in contributions if x["approval_uuid"] is None or x["approval_uuid"] not in removed]
    live = {**_live_limits(purchase), **{name: getattr(purchase, name) for name in QUEUE_FIELDS},
            "reserved_created_at": purchase.reserved_created_at, "used_bytes": int(purchase.used_bytes or 0),
            "status": getattr(purchase.status, "value", purchase.status)}

    def later_starts(after_seq):
        starts = [("activation", a) for a in activations if a["event_seq"] > after_seq] + [
            ("contribution", x) for x in effective if x["mode"] == "immediate_reset" and x["event_seq"] > after_seq]
        return [s for s in starts if f"{s[0]}:{s[1]['id']}" not in undone]

    def first_start(after_seq):
        starts = later_starts(after_seq)
        return min(starts, key=lambda s: s[1]["event_seq"]) if starts else None

    def used_at(start) -> Optional[int]:
        kind, row = start
        if kind == "activation":
            return int(row["used_bytes_before"])
        return int(json.loads(row["before_json"]).get("used_bytes") or 0)

    if c["mode"] == "reserved":
        batch = int(c["batch_seq"])
        consumed_by = next((a for a in activations if int(a["seq_before"]) == batch), None)
        batch_all = [x for x in contributions if x["mode"] in ("reserved", "transfer") and x["batch_seq"] == batch]
        batch_rows = [x for x in batch_all if x in effective]
        if consumed_by is None:
            if int(purchase.reservation_activation_seq or 0) != batch:
                raise RenewalRefused("renewal_history_inconsistent")
            if not _empty(_queue_of(json.loads(batch_all[0]["before_json"]))):
                raise RenewalRefused("renewal_queue_base_unknown")
            if _norm_queue(_replay(batch_rows)) != _norm_queue(_queue_of(live)):
                raise RenewalRefused("renewal_queue_changed_outside_history")
            remaining = [x for x in batch_rows if x["id"] != c["id"]]
            queue = _replay(remaining)
            if _empty(queue):
                queue, created = {name: None for name in QUEUE_FIELDS}, None
            elif batch_rows[0]["id"] == c["id"]:          # the queue's time is its first remaining payment's
                created = remaining[0]["created_at"]
            else:
                created = purchase.reserved_created_at
            before = {**_queue_of(live), "reserved_created_at": purchase.reserved_created_at}
            after = {**queue, "reserved_created_at": created}
            return {"case": "queued", "purchase": purchase,
                    "bound": {**base, "case": "queued", "live_queue": _norm_queue(_queue_of(live)),
                              "queue_after": _norm_queue(queue), "batch": [int(x["id"]) for x in batch_rows]},
                    "before": before, "after": after, "consumption": _consumption(0, None, False)}
        start_seq, term_after = int(consumed_by["event_seq"]), json.loads(consumed_by["after_json"])
        term_before = json.loads(consumed_by["before_json"])
        prior_used = int(consumed_by["used_bytes_before"])
        shared = len(batch_rows) != 1
        start_id = ("activation", int(consumed_by["id"]))
    else:                                                       # immediate_reset
        start_seq, term_after = int(c["event_seq"]), json.loads(c["after_json"])
        term_before = json.loads(c["before_json"])
        prior_used = int(term_before.get("used_bytes") or 0)
        shared = False
        start_id = ("contribution", int(c["id"]))
    term_quota = int(term_after.get("quota_bytes") or 0)
    nxt = first_start(start_seq)
    if nxt is not None:
        return {"case": "superseded", "purchase": purchase,
                "bound": {**base, "case": "superseded", "term_start": list(start_id),
                          "next_start": [nxt[0], int(nxt[1]["id"])], "shared": shared},
                "before": None, "after": None, "consumption": _consumption(term_quota, used_at(nxt), shared)}
    if shared:
        raise RenewalRefused("renewal_term_shared")
    expected = _limits(term_after)
    if expected["quota_bytes"] > 0:
        expected["quota_bytes"] += _own_quota_grants(db, approval_uuid, purchase.id)
    if not _same_limits(_live_limits(purchase), expected):
        raise RenewalRefused("renewal_term_changed")
    restored = _limits(term_before)
    before = {**_live_limits(purchase), "used_bytes": live["used_bytes"], "status": live["status"]}
    after = {**restored, "used_bytes": prior_used}
    return {"case": "current_term", "purchase": purchase,
            "bound": {**base, "case": "current_term", "term_start": list(start_id),
                      "term_limits": [str(v) for v in _limits(term_after).values()],
                      "restore": [str(v) for v in restored.values()], "prior_used": prior_used},
            "before": before, "after": after,
            "consumption": _consumption(term_quota, live["used_bytes"], False)}


def public(p: dict) -> dict:
    """The preview's view of a plan (no ORM object)."""
    out = {k: v for k, v in p.items() if k != "purchase"}
    out["note"] = SCOPE_NOTE
    out["bound_hash"] = bound_hash(p)
    return out


def bound_hash(p: dict) -> str:
    return hashlib.sha256(json.dumps(p["bound"], sort_keys=True, default=str).encode()).hexdigest()


# ------------------------------------------------------------------ the hold (block step) and the step
def hold(db, ctx: dict, operation_id: int) -> dict:
    """In the block step, before any reversal: the plan again under the
    purchase row lock, the same as confirmed, and - for a queue or a current
    term - a hold on the service until the renewal step commits: the poller
    and RADIUS do not start its queue, and it is not renewed (409
    purchase_void_in_progress), so nothing the reversal depends on moves
    between the void's own steps. A stop in between keeps the hold; the
    operation's retry resumes it."""
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == ctx["approval_uuid"])).mappings().one()
    current = plan(db, dict(approval), locked=True)
    if bound_hash(current) != (ctx.get("renewal") or {}).get("bound_hash"):
        raise RenewalRefused("renewal_state_changed")
    if current["case"] not in ("queued", "current_term"):
        return {"held": False, "case": current["case"]}
    table = rt.purchase_void_holds
    exists = db.execute(select(table.c.id).where(table.c.approval_uuid == ctx["approval_uuid"])).first()
    if exists is None:
        db.execute(table.insert().values(purchase_key=current["bound"]["purchase_key"], approval_uuid=ctx["approval_uuid"],
                                         void_operation_id=operation_id, state="active", created_at=dt.datetime.utcnow()))
    return {"held": True, "case": current["case"]}


def _release(db, approval_uuid: str, now) -> None:
    table = rt.purchase_void_holds
    db.execute(table.update().where(table.c.approval_uuid == approval_uuid, table.c.state == "active").values(
        state="released", released_at=now))


def apply(db, ctx: dict, operation_id: int) -> dict:
    """In the void's step transaction: the plan again under the purchase row
    lock, the same as confirmed, then the change and its durable record."""
    from . import receipt_void_effects as void_effects
    done = db.execute(select(rt.receipt_void_renewal_reversals).where(
        rt.receipt_void_renewal_reversals.c.approval_uuid == ctx["approval_uuid"])).mappings().first()
    if done is not None:
        return {"reversal_id": int(done["id"]), "case": done["reversal_case"], "replayed": True}
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == ctx["approval_uuid"])).mappings().one()
    if approval["target_shape"] != "renew_purchase":
        raise RenewalRefused("renewal_void_shape_invalid")
    current = plan(db, dict(approval), locked=True)
    if bound_hash(current) != (ctx.get("renewal") or {}).get("bound_hash"):
        raise RenewalRefused("renewal_state_changed")
    purchase, case = current["purchase"], current["case"]
    now = dt.datetime.utcnow()
    consumption = current["consumption"]
    if case == "queued":
        for name in QUEUE_FIELDS:
            setattr(purchase, name, current["after"][name])
        purchase.reserved_created_at = current["after"]["reserved_created_at"]
        # the reversed value: this payment's queued volume (0 for days / unlimited only)
        state, value = "fully_reversed", int(current["bound"]["share"]["add_quota_bytes"])
    elif case == "current_term":
        after = current["after"]
        purchase.quota_bytes = after["quota_bytes"]
        purchase.expire_at = after["expire_at"]
        purchase.package_id = after["package_id"]
        purchase.max_concurrent_sessions = after["max_concurrent_sessions"]
        purchase.used_bytes = after["used_bytes"]           # the previous term's own usage, never the voided term's
        if purchase.status != models.UserStatus.disabled:
            purchase.status = user_ops._resolve_status_after_reset(purchase.quota_bytes, purchase.used_bytes,
                                                                   purchase.expire_at, now)
        db.info.setdefault(void_effects.RECONCILE, []).append(("Purchase", purchase.id))
        state = "partially_reversed" if consumption["consumed_bytes"] else "fully_reversed"
        value = int(consumption["set_aside_bytes"] or 0)        # the unused share taken back
    elif case == "superseded":
        state, value = "unrecoverable", 0                     # the term had already been replaced
    else:
        state, value = "resource_deleted", None
    start = current["bound"].get("term_start")
    row_id = db.execute(rt.receipt_void_renewal_reversals.insert().values(
        approval_uuid=ctx["approval_uuid"], void_operation_id=operation_id, purchase_id=current["bound"]["purchase_id"],
        purchase_key=current["bound"]["purchase_key"], reversal_case=case,
        term_start=f"{start[0]}:{start[1]}" if case == "current_term" and start else None,
        before_json=json.dumps(current["before"], sort_keys=True, default=str),
        after_json=json.dumps(current["after"], sort_keys=True, default=str), created_at=now,
        **consumption)).inserted_primary_key[0]
    _release(db, ctx["approval_uuid"], now)
    effects = rv.receipt_approval_effects
    for effect in db.execute(select(effects).where(effects.c.approval_uuid == ctx["approval_uuid"],
                                                   effects.c.effect_type == "purchase_renewed",
                                                   effects.c.voided_at.is_(None))).mappings().all():
        void_effects.mark_effect_reversed(db, effect["id"], state, value, operation_id)
    db.flush()
    return {"reversal_id": int(row_id), "case": case, **consumption}
