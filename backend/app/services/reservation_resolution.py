"""Case-by-case resolution of a User-level reservation (addendum
receipt-void-addendum-p9a-reservations-2026-10-19.md), before the joint
activation only. Nothing here decides anything: every change is the
execution of ONE explicit, recorded owner decision.

  candidates()   read-only evidence per reserved customer; the classification
                 (rp_only / matched / ambiguous) is display only and never
                 enables or suggests an action;
  decide()       records the owner's explicit decision - action, target, cap
                 policy, reason, evidence - with the bound state it was made on;
  preview()      the decision's exact before/after against the current state;
                 a state that changed since the decision -> 409 state_changed;
  execute()      under the locks, the same state again, then the change and its
                 audit row in ONE commit. The same idempotency key with the same
                 request returns the stored result (even if the state moved on
                 since); with another request -> 409. A decision runs once.

Bound state: what the decision depends on - the customer's identity,
ownership and reservation, its legacy connections, every purchase's identity,
owner and queue, the target's limits, the reserved package, the cited ledger
rows and the runtime mode. Never usage: the poller moves it every minute and
it changes neither action.

Lock order (the activation's): wallet_runtime_state (shared) ->
receipt_approval_runtime_state (shared; the activation holds it exclusively)
-> the User -> its purchases in id order. No ledger, balance, loyalty count or
node is touched by any action.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, OperationalError

from .. import models, models_receipt_void as rv, models_reservation_transition as rt
from ..config import settings
from . import receipt_void_schema

TOKEN_SECONDS = 600
ATTEMPTS = 3
TRANSIENT_MARIADB_ERRORS = (1205, 1213)
USER_FIELDS = ("reserved_quota_bytes", "reserved_duration_days", "reserved_package_id", "reserved_created_at")
HISTORICAL_SEMANTICS = ("transferred as a historical reservation: reserved_replace_package_limits stays NULL, so "
                        "a zero quota or duration means 'unchanged', never unlimited; it starts only when the "
                        "target's current quota or expiry ends")


def _stamp(value):
    return value.isoformat(sep=" ")[:19] if isinstance(value, dt.datetime) else value


def _hash(payload) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _reserved(row) -> bool:
    return bool(row.reserved_quota_bytes or row.reserved_duration_days or row.reserved_package_id is not None)


def _sqlite(db) -> bool:
    return db.get_bind().dialect.name == "sqlite"


# ------------------------------------------------------------------ state
def _user_or_404(db, user_id, lock=False) -> models.User:
    query = select(models.User).where(models.User.id == user_id).execution_options(populate_existing=True)
    if lock and not _sqlite(db):
        query = query.with_for_update()
    user = db.execute(query).scalar_one_or_none()
    if user is None:
        raise HTTPException(404, "user_not_found")
    return user


def _purchases(db, user_id, lock=False) -> list:
    query = select(models.Purchase).where(models.Purchase.user_id == user_id).order_by(
        models.Purchase.id).execution_options(populate_existing=True)
    if lock and not _sqlite(db):
        query = query.with_for_update()
    return list(db.execute(query).scalars())


def _runtime(db, lock=False) -> dict:
    wallet = select(rv.wallet_runtime_state.c.phase).where(rv.wallet_runtime_state.c.id == 1)
    state = select(rv.receipt_approval_runtime_state).where(rv.receipt_approval_runtime_state.c.id == 1)
    if lock and not _sqlite(db):
        wallet, state = wallet.with_for_update(read=True), state.with_for_update(read=True)
    db.execute(wallet).first()
    row = db.execute(state).mappings().one()
    return {"registration_mode": row["registration_mode"], "loyalty_timing_mode": row["loyalty_timing_mode"],
            "completeness_generation": int(row["completeness_generation"])}


def _require_before_activation(runtime: dict) -> None:
    if runtime["registration_mode"] == "required" or runtime["loyalty_timing_mode"] == "payment_event":
        raise HTTPException(409, "activation_active")


def state(db, user_id, *, target_purchase_id=None, delivered_by=(), ledger_ids=(), lock=False) -> dict:
    """The bound state of one decision (see the module docstring)."""
    runtime = _runtime(db, lock=lock)
    user = _user_or_404(db, user_id, lock=lock)
    purchases = _purchases(db, user.id, lock=lock)
    legacy = sorted(c.id for c in db.query(models.Connection.id).filter(
        models.Connection.user_id == user.id, models.Connection.purchase_id.is_(None)))
    package = db.get(models.Package, user.reserved_package_id) if user.reserved_package_id is not None else None
    by_id = {p.id: p for p in purchases}
    target = by_id.get(target_purchase_id) if target_purchase_id is not None else None
    ledger = []
    for entry_id in sorted(set(ledger_ids)):
        entry = db.get(models.LedgerEntry, entry_id)
        ledger.append(None if entry is None else {"id": entry.id, "amount": int(entry.amount or 0), "kind": entry.kind,
                                                  "user_id": entry.user_id, "purchase_id": entry.purchase_id,
                                                  "voided_at": _stamp(entry.voided_at)})
    return {
        "runtime": runtime,
        "user": {"id": user.id, "username": user.username, "telegram_id": user.telegram_id,
                 "owner_admin_id": user.owner_admin_id, **{f: _stamp(getattr(user, f)) for f in USER_FIELDS}},
        "legacy_connection_ids": legacy,
        "purchases": [{"id": p.id, "created_at": _stamp(p.created_at), "user_id": p.user_id, "package_id": p.package_id,
                       "reserved": _reserved(p)} for p in purchases],
        "target": None if target is None else {
            "id": target.id, "created_at": _stamp(target.created_at), "user_id": target.user_id,
            "status": getattr(target.status, "value", target.status), "package_id": target.package_id,
            "quota_bytes": int(target.quota_bytes or 0), "expire_at": _stamp(target.expire_at),
            "max_concurrent_sessions": target.max_concurrent_sessions,
            "reserved": _reserved(target), "reservation_activation_seq": int(target.reservation_activation_seq or 0)},
        "target_missing": target_purchase_id is not None and target is None,
        "delivered_by": [None if by_id.get(pid) is None else {
            "id": pid, "created_at": _stamp(by_id[pid].created_at), "package_id": by_id[pid].package_id,
            "quota_bytes": int(by_id[pid].quota_bytes or 0)} for pid in sorted(set(delivered_by))],
        "reserved_package": None if user.reserved_package_id is None else (
            {"id": user.reserved_package_id, "exists": False} if package is None else
            {"id": package.id, "exists": True, "quota_gb": package.quota_gb, "duration_days": package.duration_days,
             "max_concurrent_sessions": package.max_concurrent_sessions}),
        "ledger": ledger,
    }


def _plan(decision: dict, current: dict) -> dict:
    """before/after of exactly this decision, or 409 with the reason."""
    user = current["user"]
    if not any(user[f] is not None for f in ("reserved_quota_bytes", "reserved_duration_days", "reserved_package_id")):
        raise HTTPException(409, "user_not_reserved")
    if current["legacy_connection_ids"]:
        # a live user-level pool still activates its reservation itself
        raise HTTPException(409, "user_has_legacy_connections")
    before = {"user": {f: user[f] for f in USER_FIELDS}}
    after = {"user": {f: None for f in USER_FIELDS}}
    warnings = []
    if decision["action"] == "transfer_to_purchase":
        target = current["target"]
        if target is None or current["target_missing"] or target["user_id"] != user["id"]:
            raise HTTPException(409, "transfer_target_invalid")
        if target["reserved"]:
            raise HTTPException(409, "transfer_target_reserved")
        if target["status"] == models.UserStatus.disabled.value:
            raise HTTPException(409, "transfer_target_disabled")
        package = current["reserved_package"]
        if package is not None and not package["exists"]:
            raise HTTPException(409, "reserved_package_missing")
        if decision["cap_policy"] == "package_cap":
            if package is None or package["max_concurrent_sessions"] is None:
                raise HTTPException(409, "package_cap_unavailable")
            cap = int(package["max_concurrent_sessions"])
        else:
            cap = None
        before["target"] = {"id": target["id"], "reserved": False,
                            "max_concurrent_sessions": target["max_concurrent_sessions"]}
        after["target"] = {"id": target["id"], "reserved_quota_bytes": user["reserved_quota_bytes"],
                           "reserved_duration_days": user["reserved_duration_days"],
                           "reserved_package_id": user["reserved_package_id"],
                           "reserved_created_at": user["reserved_created_at"],
                           "reserved_max_concurrent_sessions": cap, "reserved_replace_package_limits": None,
                           "cap_on_activation": target["max_concurrent_sessions"] if cap is None else cap}
        warnings.append(HISTORICAL_SEMANTICS)
        if cap is None:
            warnings.append("cap_policy keep_current_cap: the service keeps its current cap "
                            f"({target['max_concurrent_sessions']}) when the reservation starts")
    else:
        if decision["delivered_by"]:
            if any(item is None for item in current["delivered_by"]):
                raise HTTPException(409, "delivered_by_purchase_invalid")
        warnings.append("retire_residual: the reserved fields are cleared; no service, quota, credit or count changes")
    return {"before": before, "after": after, "warnings": warnings}


# ------------------------------------------------------------------ candidates (read-only)
def candidates(db) -> list[dict]:
    """Every reserved customer with the evidence; no action is suggested."""
    out = []
    users = db.query(models.User).filter((models.User.reserved_quota_bytes > 0) | (models.User.reserved_duration_days > 0)
                                         | models.User.reserved_package_id.isnot(None)).order_by(models.User.id).all()
    for user in users:
        purchases = db.query(models.Purchase).filter(models.Purchase.user_id == user.id).order_by(models.Purchase.id).all()
        matched = []
        if user.reserved_created_at is not None:
            low = user.reserved_created_at - dt.timedelta(seconds=5)
            for purchase in purchases:
                conns = [c for c in purchase.connections if c.created_at and low <= c.created_at <= user.reserved_created_at]
                if conns:
                    matched.append({"purchase_id": purchase.id, "package_id": purchase.package_id,
                                    "connections_created_at": [_stamp(c.created_at) for c in conns]})
        amount = bool(user.reserved_quota_bytes or user.reserved_duration_days)
        out.append({
            "user_id": user.id, "username": user.username,
            "reservation": {f: _stamp(getattr(user, f)) for f in USER_FIELDS},
            "legacy_connections": sum(1 for c in user.connections if c.purchase_id is None),
            "purchases": [{"id": p.id, "package_id": p.package_id, "status": getattr(p.status, "value", p.status),
                           "created_at": _stamp(p.created_at)} for p in purchases],
            "time_matched_purchases": matched,
            "classification": "rp_only" if not amount else ("matched" if matched else "ambiguous"),
            "note": "display only: no classification enables or suggests an action",
        })
    return out


# ------------------------------------------------------------------ decision
def decide(db, admin, *, user_id, action, reason, target_purchase_id=None, cap_policy=None,
           delivered_by_purchase_ids=None, evidence_refs=None) -> dict:
    """Commits one explicit decision. Nothing is changed by it."""
    receipt_void_schema.assert_ready()
    if action not in rt.RESOLUTION_ACTIONS:
        raise HTTPException(422, "action_invalid")
    if not isinstance(reason, str) or len(reason.strip()) < 10:
        raise HTTPException(422, "reason_required")
    if action == "transfer_to_purchase":
        if target_purchase_id is None or cap_policy not in rt.CAP_POLICIES:
            raise HTTPException(422, "transfer_requires_target_and_cap_policy")
        if delivered_by_purchase_ids:
            raise HTTPException(422, "delivered_by_not_applicable")
    elif target_purchase_id is not None or cap_policy is not None:
        raise HTTPException(422, "retire_takes_no_target")
    delivered = sorted({int(i) for i in (delivered_by_purchase_ids or [])})
    refs = evidence_refs or {}
    ledger_ids = sorted({int(i) for i in refs.get("ledger_entry_ids", [])})
    current = state(db, user_id, target_purchase_id=target_purchase_id, delivered_by=delivered, ledger_ids=ledger_ids)
    _require_before_activation(current["runtime"])
    decision = {"action": action, "cap_policy": cap_policy, "delivered_by": delivered}
    _plan(decision, current)
    row_id = db.execute(rt.reservation_resolution_decisions.insert().values(
        user_id=user_id, username_snapshot=current["user"]["username"], action=action,
        target_purchase_id=target_purchase_id, cap_policy=cap_policy,
        delivered_by_json=json.dumps(delivered) if delivered else None, reason=reason.strip(),
        evidence_json=json.dumps(refs, sort_keys=True, default=str),
        state_json=json.dumps(current, sort_keys=True, default=str), state_hash=_hash(current),
        actor_admin_id=admin.id, created_at=dt.datetime.utcnow())).inserted_primary_key[0]
    db.commit()
    return {"decision_id": int(row_id), "state_hash": _hash(current), "state": current}


def _decision(db, decision_id) -> dict:
    row = db.execute(select(rt.reservation_resolution_decisions).where(
        rt.reservation_resolution_decisions.c.id == decision_id)).mappings().first()
    if row is None:
        raise HTTPException(404, "decision_not_found")
    decision = dict(row)
    decision["delivered_by"] = json.loads(decision["delivered_by_json"]) if decision["delivered_by_json"] else []
    decision["ledger_ids"] = json.loads(decision["evidence_json"]).get("ledger_entry_ids", [])
    return decision


def _current_for(db, decision, lock=False) -> dict:
    return state(db, decision["user_id"], target_purchase_id=decision["target_purchase_id"],
                 delivered_by=decision["delivered_by"], ledger_ids=decision["ledger_ids"], lock=lock)


# ------------------------------------------------------------------ preview / token
def _sign(body: bytes) -> str:
    return hmac.new(settings.secret_key.encode(), b"reservation-resolution|" + body, hashlib.sha256).hexdigest()


def _token(decision_id: int, digest: str) -> str:
    body = json.dumps({"d": decision_id, "h": digest, "exp": int(time.time() + TOKEN_SECONDS)},
                      sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + _sign(body)


def _read_token(token) -> dict:
    try:
        encoded, signature = str(token).split(".", 1)
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(body)):
            raise ValueError("signature")
        data = json.loads(body)
    except (ValueError, TypeError):
        raise HTTPException(409, "preview_token_invalid") from None
    if int(data.get("exp", 0)) < int(time.time()):
        raise HTTPException(409, "preview_expired")
    return data


def preview(db, decision_id: int) -> dict:
    receipt_void_schema.assert_ready()
    decision = _decision(db, decision_id)
    if _executed(db, decision_id) is not None:
        raise HTTPException(409, "decision_already_executed")
    current = _current_for(db, decision)
    _require_before_activation(current["runtime"])
    digest = _hash(current)
    if digest != decision["state_hash"]:
        raise HTTPException(409, "state_changed")
    plan = _plan(decision, current)
    return {"decision_id": decision_id, "action": decision["action"], "user_id": decision["user_id"],
            "username": current["user"]["username"], "target_purchase_id": decision["target_purchase_id"],
            "cap_policy": decision["cap_policy"], "reason": decision["reason"], **plan, "state": current,
            "preview_token": _token(decision_id, digest), "token_expires_in": TOKEN_SECONDS}


# ------------------------------------------------------------------ execute
def _executed(db, decision_id):
    return db.execute(select(rt.reservation_resolutions).where(
        rt.reservation_resolutions.c.decision_id == decision_id)).mappings().first()


def _stored(db, key: str):
    return db.execute(select(rt.reservation_resolutions).where(
        rt.reservation_resolutions.c.idempotency_key == key)).mappings().first()


def _replay(row, request_hash) -> dict:
    if row["request_hash"] != request_hash:
        raise HTTPException(409, "idempotency_key_reused")
    return {**json.loads(row["result_json"]), "replayed": True}


def execute(db, admin, *, decision_id: int, preview_token: str, typed_username: str, idempotency_key: str,
            ip: Optional[str] = None) -> dict:
    receipt_void_schema.assert_ready()
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(422, "idempotency_key_required")
    request_hash = _hash({"decision_id": int(decision_id), "typed_username": typed_username})
    for attempt in range(1, ATTEMPTS + 1):
        try:
            return _execute_once(db, admin, int(decision_id), preview_token, typed_username, idempotency_key,
                                 request_hash, ip)
        except OperationalError as exc:
            # _execute_once rolled the whole transaction back: start over
            code = getattr(getattr(exc, "orig", None), "args", (None,))[0]
            if _sqlite(db) or code not in TRANSIENT_MARIADB_ERRORS:
                raise
            if attempt == ATTEMPTS:
                raise HTTPException(503, "resolution_retry_required") from exc
            time.sleep(0.2 * attempt)


def _execute_once(db, admin, decision_id, preview_token, typed_username, key, request_hash, ip) -> dict:
    db.rollback()
    stored = _stored(db, key)                     # before any state check: a retry returns what happened
    if stored is not None:
        db.rollback()
        return _replay(stored, request_hash)
    decision = _decision(db, decision_id)
    if _executed(db, decision_id) is not None:
        db.rollback()
        raise HTTPException(409, "decision_already_executed")
    token = _read_token(preview_token)
    if int(token.get("d", -1)) != decision_id or token.get("h") != decision["state_hash"]:
        raise HTTPException(409, "preview_token_invalid")
    db.rollback()
    if _sqlite(db):
        db.execute(text("BEGIN IMMEDIATE"))
    try:
        current = _current_for(db, decision, lock=True)
        # under the locks again: a concurrent request may have committed since
        stored = _stored(db, key)
        if stored is not None:
            db.rollback()
            return _replay(stored, request_hash)
        if _executed(db, decision_id) is not None:
            raise HTTPException(409, "decision_already_executed")
        _require_before_activation(current["runtime"])
        if typed_username != current["user"]["username"]:
            raise HTTPException(422, "typed_username_mismatch")
        if _hash(current) != decision["state_hash"]:
            raise HTTPException(409, "state_changed")
        plan = _plan(decision, current)
        user = db.get(models.User, decision["user_id"])
        if decision["action"] == "transfer_to_purchase":
            target = db.get(models.Purchase, decision["target_purchase_id"])
            values = plan["after"]["target"]
            target.reserved_quota_bytes = values["reserved_quota_bytes"]
            target.reserved_duration_days = values["reserved_duration_days"]
            target.reserved_package_id = values["reserved_package_id"]
            target.reserved_created_at = user.reserved_created_at
            target.reserved_max_concurrent_sessions = values["reserved_max_concurrent_sessions"]
            target.reserved_replace_package_limits = None
        user.reserved_quota_bytes = None
        user.reserved_duration_days = None
        user.reserved_package_id = None
        user.reserved_created_at = None
        result = {"resolution": None, "decision_id": decision_id, "action": decision["action"],
                  "user_id": decision["user_id"], "target_purchase_id": decision["target_purchase_id"],
                  "before": plan["before"], "after": plan["after"]}
        resolution_id = db.execute(rt.reservation_resolutions.insert().values(
            idempotency_key=key, request_hash=request_hash, decision_id=decision_id, user_id=decision["user_id"],
            action=decision["action"], target_purchase_id=decision["target_purchase_id"],
            before_json=json.dumps(plan["before"], sort_keys=True, default=str),
            after_json=json.dumps(plan["after"], sort_keys=True, default=str),
            result_json="{}", actor_admin_id=admin.id, ip=(ip or None) and str(ip)[:64],
            created_at=dt.datetime.utcnow())).inserted_primary_key[0]
        result["resolution"] = int(resolution_id)
        table = rt.reservation_resolutions
        db.execute(table.update().where(table.c.id == resolution_id).values(
            result_json=json.dumps(result, sort_keys=True, default=str)))
        db.commit()                                   # the change and its audit row: one commit
        return result
    except IntegrityError:
        db.rollback()
        stored = _stored(db, key)                     # the same key committed by a concurrent request
        if stored is not None:
            return _replay(stored, request_hash)
        raise HTTPException(409, "decision_already_executed")
    except BaseException:
        db.rollback()
        raise
