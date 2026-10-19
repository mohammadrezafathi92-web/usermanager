"""What a receipt void would touch, read-only (Receipt Void 13.1, 17, 18
"Preview", 12.6 projections). Used by the preview (P10), and again by the
execute to prove nothing changed since (the preview token's state hash).

  find()        approval_uuid or ledger_entry_id -> the approval row, for a
                caller who may see it (owner snapshot in visible_admin_ids;
                no owner: superadmin only). Missing and not allowed are
                both 404; a ledger row without an approval is 409.
  eligible()    completed, registered 'required', completeness_generation
                >= 1 - anything else 409 approval_not_voidable_generation /
                approval_not_voidable_state.
  scope()       the resources: new_user / renew_* the whole account,
                existing_user the Purchase it created and its connections,
                topup nothing. Never a credential: ids, protocol, node name.
  preview()     everything the operator must see before deciding, plus a
                signed, expiring preview_token over the state hash.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_receipt_void as rv
from ..config import settings
from . import accounting, payment_card_events, wallet_lots

TOKEN_SECONDS = 600
FULL_ACCOUNT = ("new_user", "renew_purchase", "renew_user")


# ------------------------------------------------------------------ lookup and authorization (17)
def find(db, admin: models.AdminUser, *, approval_uuid: Optional[str] = None,
         ledger_entry_id: Optional[int] = None, lock: bool = False) -> dict:
    if (approval_uuid is None) == (ledger_entry_id is None):
        raise HTTPException(422, "approval_uuid_or_ledger_entry_id")
    if ledger_entry_id is not None:
        entry = db.get(models.LedgerEntry, ledger_entry_id)
        if entry is None or not _visible(db, admin, entry.owner_admin_id_snapshot
                                         if entry.owner_admin_id_snapshot is not None else entry.admin_id):
            raise HTTPException(404, "receipt_approval_not_found")
        if not entry.approval_uuid:
            raise HTTPException(409, "receipt_void_not_traceable")
        approval_uuid = entry.approval_uuid
    query = select(rv.receipt_approvals).where(rv.receipt_approvals.c.approval_uuid == str(approval_uuid))
    if lock:
        query = query.with_for_update()
    row = db.execute(query).mappings().first()
    if row is None or not _visible(db, admin, row["owner_admin_id_snapshot"]):
        raise HTTPException(404, "receipt_approval_not_found")
    return dict(row)


def _visible(db, admin, owner_admin_id) -> bool:
    ids = accounting.visible_admin_ids(db, admin)
    if ids is None:
        return True
    return owner_admin_id is not None and owner_admin_id in ids


def eligible(approval: dict) -> None:
    if approval["registered_under_mode"] != "required" or int(approval["completeness_generation"] or 0) < 1:
        raise HTTPException(409, "approval_not_voidable_generation")
    if approval["state"] != "completed":
        raise HTTPException(409, "approval_not_voidable_state")


# ------------------------------------------------------------------ the approval's own rows
def effects(db, approval_uuid: str) -> list[dict]:
    table = rv.receipt_approval_effects
    return [dict(row) for row in db.execute(select(table).where(table.c.approval_uuid == approval_uuid)
                                            .order_by(table.c.id)).mappings()]


def _effect(rows, effect_type, key=None):
    return next((row for row in rows if row["effect_type"] == effect_type and (key is None or row["effect_key"] == key)),
                None)


def target_user_id(approval: dict, rows: list[dict]) -> Optional[int]:
    if approval["target_shape"] == "new_user":
        created = _effect(rows, "user_created", "user")
        return int(created["resource_id"]) if created and created["resource_id"] is not None else None
    return approval["target_user_id_snapshot"]


def scope(db, approval: dict, rows: list[dict]) -> dict:
    """The resources of 13.1, by id (connections with node name, no credential)."""
    shape, user_id = approval["target_shape"], target_user_id(approval, rows)
    user = db.get(models.User, user_id) if user_id is not None else None
    if user is not None and user.username != approval["target_username_snapshot"]:
        # The approval's customer is gone and its id was given to someone
        # else (SQLite reuses the highest rowid): never touch that customer.
        user = None
    out = {"shape": shape, "user_id": user_id, "user_exists": user is not None, "delete": None,
           "user": None, "purchases": [], "connections": [], "prior_rights": None,
           "customer_identity_id": _identity(db, user_id, approval["target_username_snapshot"], user is not None)}
    if user is None:
        return out
    out["user"] = {"id": user.id, "username": user.username, "status": getattr(user.status, "value", user.status),
                   "owner_admin_id": user.owner_admin_id, "telegram_id": user.telegram_id,
                   "total_quota_bytes": int(user.total_quota_bytes or 0), "expire_at": user.expire_at,
                   "reserved_quota_bytes": user.reserved_quota_bytes, "reserved_duration_days": user.reserved_duration_days,
                   "reserved_package_id": user.reserved_package_id}
    if shape == "topup":
        return out
    if shape in FULL_ACCOUNT:
        purchases = db.query(models.Purchase).filter(models.Purchase.user_id == user.id).order_by(models.Purchase.id).all()
        connections = db.query(models.Connection).filter(models.Connection.user_id == user.id).order_by(
            models.Connection.id).all()
        out["delete"] = {"kind": "user", "id": user.id}
    else:
        created = _effect(rows, "purchase_created", "purchase")
        purchase = db.get(models.Purchase, int(created["resource_id"])) if created and created["resource_id"] else None
        purchases = [purchase] if purchase is not None else []
        connections = [] if purchase is None else db.query(models.Connection).filter(
            models.Connection.purchase_id == purchase.id).order_by(models.Connection.id).all()
        out["delete"] = {"kind": "purchase", "id": purchase.id} if purchase is not None else None
    nodes = {node.id: node.name for node in db.query(models.Node).filter(
        models.Node.id.in_({c.node_id for c in connections})).all()} if connections else {}
    packages = {pkg.id: pkg.name for pkg in db.query(models.Package).filter(
        models.Package.id.in_({p.package_id for p in purchases if p.package_id})).all()} if purchases else {}
    out["purchases"] = [{"id": p.id, "package_id": p.package_id, "package_name": packages.get(p.package_id),
                         "comment": p.comment, "status": getattr(p.status, "value", p.status),
                         "quota_bytes": int(p.quota_bytes or 0), "used_bytes": int(p.used_bytes or 0),
                         "expire_at": p.expire_at, "created_at": p.created_at,
                         "max_concurrent_sessions": p.max_concurrent_sessions,
                         "reserved_quota_bytes": p.reserved_quota_bytes,
                         "reserved_duration_days": p.reserved_duration_days,
                         "reserved_package_id": p.reserved_package_id,
                         "reserved_max_concurrent_sessions": p.reserved_max_concurrent_sessions,
                         "reserved_replace_package_limits": p.reserved_replace_package_limits,
                         "reservation_activation_seq": int(p.reservation_activation_seq or 0)} for p in purchases]
    # Option A of the P9a reservation addendum: the deletion scope is the
    # contract's own (13.1); every purchase, connection and queued renewal in
    # it, and the cutover snapshots of those purchases, are shown and bound
    # to the confirmation (fingerprint). A snapshot is what was queued at the
    # cutover - NOT a record of every right acquired before it.
    from . import reservation_transition
    out["prior_rights"] = {"cutover_snapshots": reservation_transition.snapshots_of(db, purchases),
                           "note": reservation_transition.PRIOR_RIGHTS_NOTE}
    out["connections"] = [{"id": c.id, "purchase_id": c.purchase_id, "protocol": getattr(c.type, "value", c.type),
                           "node_id": c.node_id, "node_name": nodes.get(c.node_id), "enabled": bool(c.enabled)}
                          for c in connections]
    return out


def other_approvals(db, approval: dict, resources: dict) -> list[dict]:
    """Every other approval on what this void deletes: the whole account for
    new_user / renew, the created purchase (its renewals) for existing_user."""
    approvals, effects_table = rv.receipt_approvals, rv.receipt_approval_effects
    user_id, delete = resources["user_id"], resources["delete"]
    if user_id is None:
        return []
    if delete is not None and delete["kind"] == "purchase":
        on_it = select(effects_table.c.approval_uuid).where(
            effects_table.c.resource_type == "Purchase", effects_table.c.resource_id == delete["id"])
        condition = approvals.c.approval_uuid.in_(on_it)
    else:
        created_here = select(effects_table.c.approval_uuid).where(
            effects_table.c.effect_type == "user_created", effects_table.c.resource_id == user_id)
        condition = (approvals.c.target_user_id_snapshot == user_id) | approvals.c.approval_uuid.in_(created_here)
    # A deleted customer's id can be given to a new one (SQLite reuses the
    # highest rowid): the username snapshot pins the approvals of THIS customer.
    username = (resources.get("user") or {}).get("username") or approval["target_username_snapshot"]
    rows = db.execute(select(approvals.c.approval_uuid, approvals.c.kind, approvals.c.target_shape, approvals.c.state,
                             approvals.c.version, approvals.c.amount_snapshot, approvals.c.completed_at).where(
        approvals.c.approval_uuid != approval["approval_uuid"], condition,
        approvals.c.target_username_snapshot == username).order_by(approvals.c.id)).mappings().all()
    return [dict(row) for row in rows]


def _identity(db, user_id: Optional[int], username: str, live: bool) -> Optional[int]:
    """The approval's customer identity: its live account, or - the customer
    deleted - the tombstoned account with the same id and username snapshot."""
    if user_id is None:
        return None
    accounts = rv.wallet_accounts
    if live:
        return db.execute(select(accounts.c.customer_identity_id).where(accounts.c.user_id == user_id)).scalar()
    return db.execute(select(accounts.c.customer_identity_id).where(
        accounts.c.user_id.is_(None), accounts.c.user_id_snapshot == user_id,
        accounts.c.username_snapshot == username).order_by(accounts.c.id.desc())).scalar()


def customer_identity(db, resources: dict) -> Optional[int]:
    return resources["customer_identity_id"]


# ------------------------------------------------------------------ loyalty: the same plan as the execution (14.2)
def loyalty_plan(db, approval: dict, resources: dict) -> dict:
    from . import receipt_void_effects
    events = rv.loyalty_purchase_events
    ids = [int(r.id) for r in db.execute(select(events.c.id).where(
        events.c.approval_uuid == approval["approval_uuid"], events.c.state == "active").order_by(events.c.id))]
    if approval["target_shape"] == "topup" or resources["user_id"] is None:
        return {"purchase_events": ids, "active_count_after": None, "rewards": []}
    user_id = resources["user_id"] if resources["user_exists"] else None
    plan = receipt_void_effects.loyalty_plan(db, user_id=user_id, user_id_snapshot=int(resources["user_id"]),
                                             void_event_ids=ids)
    return {"purchase_events": ids, "active_count_after": plan["active_count_after"],
            "rewards": [{"id": int(r["id"]), "epoch_id": int(r["epoch_id"]), "slot": int(r["slot"]),
                         "required_active_count": int(r["required_active_count"]),
                         "causation_approval_uuid": r["causation_approval_uuid"],
                         "caused_by_this_approval": r["causation_approval_uuid"] == approval["approval_uuid"],
                         "credit_amount": int(r["credit_amount"]), "quota_bytes": int(r["quota_bytes"]),
                         "wallet_credit_source_id": r["wallet_credit_source_id"]} for r in plan["rewards"]]}


# ------------------------------------------------------------------ money (12.6 projections, 14)
def sources(db, approval_uuid: str) -> list[dict]:
    """S* of wallet_reversal: the sources this approval caused, except
    loyalty rewards (reconciled by 5a, whichever reward the count no longer
    backs)."""
    table = rv.wallet_credit_sources
    return [dict(row) for row in db.execute(select(table).where(
        table.c.causation_approval_uuid == approval_uuid, table.c.origin_kind != "loyalty_reward")
        .order_by(table.c.id)).mappings()]


def _source(db, source_id) -> dict:
    return dict(db.execute(select(rv.wallet_credit_sources).where(
        rv.wallet_credit_sources.c.id == source_id)).mappings().one())


def _project(db, source: dict, identity_id: Optional[int], step: str, reward_id=None) -> dict:
    lots, apps, allocations = rv.wallet_lots, rv.wallet_credit_applications, rv.wallet_allocations
    source_lots = [dict(r) for r in db.execute(select(lots).where(
        lots.c.source_credit_source_id == source["id"]).order_by(lots.c.id)).mappings()]
    settlements = [dict(r) for r in db.execute(select(apps).where(
        apps.c.wallet_credit_source_id == source["id"], apps.c.application_kind == "debt_settlement")).mappings()]
    clawback = debt = writeoff = 0
    spent = []
    for lot in source_lots:
        if lot["state"] == "voided":
            continue
        clawback += int(lot["available_amount"])
        consumed = int(lot["consumed_amount"])
        owner = wallet_lots.account(db, lot["wallet_account_id"])["customer_identity_id"]
        if consumed > 0:
            if identity_id is not None and owner == identity_id:
                debt += consumed
            else:
                writeoff += consumed
        spent += [{"allocation_id": int(r.id), "debit_id": int(r.wallet_debit_event_id),
                   "amount": int(r.allocated_amount)} for r in db.execute(select(allocations).where(
                       allocations.c.wallet_lot_id == lot["id"]).order_by(allocations.c.id))]
    return {"source_id": source["id"], "origin_kind": source["origin_kind"], "state": source["state"],
            "step": step, "loyalty_reward_id": reward_id, "causation_approval_uuid": source["causation_approval_uuid"],
            "gross": int(source["gross_amount"]),
            "projected_clawback": clawback, "projected_debt": debt, "projected_writeoff": writeoff,
            "projected_settlement_reopen": sum(int(a["amount"]) - int(a["reversed_amount"]) for a in settlements),
            "spent_by_debits": sorted({a["debit_id"] for a in spent}), "allocations": spent,
            "spent_sales_stay_valid": True}


def wallet_projection(db, approval_uuid: str, identity_id: Optional[int], plan: dict) -> dict:
    items = [_project(db, source, identity_id, "wallet_reversal") for source in sources(db, approval_uuid)]
    for reward in plan["rewards"]:
        if reward["credit_amount"] > 0 and reward["wallet_credit_source_id"] is not None:
            items.append(_project(db, _source(db, reward["wallet_credit_source_id"]), identity_id,
                                  "loyalty_reconciliation", reward["id"]))
    return {"sources": items, "open_debt": wallet_lots.open_debt(db, identity_id) if identity_id else 0}


def ledger(db, approval_uuid: str, rows: list[dict]) -> dict:
    entries = db.query(models.LedgerEntry).filter(models.LedgerEntry.approval_uuid == approval_uuid).order_by(
        models.LedgerEntry.id).all()
    spend = _effect(rows, "ledger_credit_spend", "credit_spend")
    spend_entry = db.get(models.LedgerEntry, int(spend["resource_id"])) if spend and spend["resource_id"] else None
    discount = _effect(rows, "discount_redeemed")
    card = card_event(db, approval_uuid)
    return {
        "entries": [{"id": e.id, "kind": e.kind, "amount": int(e.amount or 0), "voided": e.voided_at is not None,
                     "payment_method": e.payment_method, "reversal_of_id": e.reversal_of_id,
                     "created_at": e.created_at} for e in entries],
        "voidable_sale_amount": sum(int(e.amount or 0) for e in entries
                                    if e.kind in ("sale_new", "sale_renew") and e.voided_at is None),
        "reseller_credit_refund": ({"ledger_entry_id": spend_entry.id, "admin_id": spend_entry.admin_id,
                                    "amount": int(spend_entry.amount or 0)} if spend_entry else None),
        "discount": ({"redemption_id": discount["resource_id"], "amount": discount["delta_value"]}
                     if discount else None),
        "card": ({"event_id": card["id"], "card_id": card["card_id_snapshot"], "amount": int(card["amount"]),
                  "pool_key": card["pool_key"], "reset_after": bool(card["reset_after"]),
                  "voided": card["voided_at"] is not None} if card else None),
    }


def card_event(db, approval_uuid: str):
    events = payment_card_events._tables()[2]
    return db.execute(select(events).where(events.c.approval_uuid == approval_uuid)).mappings().first()


# ------------------------------------------------------------------ the lease set (12.1)
def lease_keys(db, approval_uuid: str, identity_id: Optional[int]) -> list[str]:
    """Every customer identity the void touches (its own customer, every
    account a caused source credited - a referrer is another identity - and
    the owner of every reward the reconciliation may void) and the receipt's
    card pool; sorted: identities numerically, then pools alphabetically."""
    sources_t, accounts = rv.wallet_credit_sources, rv.wallet_accounts
    identities = {int(identity_id)} if identity_id is not None else set()
    identities |= {int(r.customer_identity_id) for r in db.execute(
        select(accounts.c.customer_identity_id).join(sources_t, sources_t.c.wallet_account_id == accounts.c.id)
        .where(sources_t.c.causation_approval_uuid == approval_uuid))}
    pools = set()
    card = card_event(db, approval_uuid)
    if card is not None:
        pools.add(card["pool_key"])
    return [f"customer_identity:{i}" for i in sorted(identities)] + [f"payment_card_pool:{p}" for p in sorted(pools)]


# ------------------------------------------------------------------ blockers known before the claim
def blockers(db, approval: dict, rows: list[dict], resources: dict) -> list[str]:
    """What would strand the void right after the claim; fail closed instead."""
    from . import receipt_void_deletion
    out = []
    for row in rows:
        if row["effect_type"] in ("ledger_sale", "ledger_topup") and row["resource_id"] is not None:
            entry = db.get(models.LedgerEntry, int(row["resource_id"]))
            if entry is not None and entry.voided_at is not None:
                out.append("approval_sale_voided_elsewhere")
    if resources["delete"] is not None and not receipt_void_deletion.durable_ready(db, resources["delete"]):
        out.append("receipt_void_deletion_not_durable")
    if resources["user_exists"] and resources["customer_identity_id"] is None:
        out.append("wallet_account_not_ready")
    return out


def preflight(db, approval: dict, rows: list[dict], resources: dict) -> None:
    found = blockers(db, approval, rows, resources)
    if found:
        raise HTTPException(409, found[0])


# ------------------------------------------------------------------ state hash and token (18)
def fingerprint(db, approval: dict, rows: list[dict], resources: dict) -> str:
    """Everything the operator confirmed that a void destroys or reverses,
    WITHOUT the approval's own version/state (our own transitions move them):
    the target user and every purchase / connection of the deletion scope
    with their mutable fields (not usage, which the poller moves), every
    other approval on that scope with its state and version, this approval's
    effects, the caused sources and their lots, the identity's debts, the
    loyalty plan, the card event. Any change -> 409 preview_state_changed."""
    lots, debts = rv.wallet_lots, rv.wallet_debts
    caused = [dict(r) for r in db.execute(select(rv.wallet_credit_sources).where(
        rv.wallet_credit_sources.c.causation_approval_uuid == approval["approval_uuid"])
        .order_by(rv.wallet_credit_sources.c.id)).mappings()]
    plan = loyalty_plan(db, approval, resources)
    reward_sources = [r["wallet_credit_source_id"] for r in plan["rewards"] if r["wallet_credit_source_id"]]
    source_ids = sorted({s["id"] for s in caused} | set(reward_sources))
    lot_rows = [] if not source_ids else [
        (int(r.id), int(r.version), r.state) for r in db.execute(select(lots.c.id, lots.c.version, lots.c.state).where(
            lots.c.source_credit_source_id.in_(source_ids)).order_by(lots.c.id))]
    identity = resources["customer_identity_id"]
    debt_rows = [] if identity is None else [(int(r.id), int(r.version)) for r in db.execute(
        select(debts.c.id, debts.c.version).where(debts.c.customer_identity_id == identity).order_by(debts.c.id))]
    card = card_event(db, approval["approval_uuid"])
    user = dict(resources["user"] or {})
    if resources["delete"] is None:
        user.pop("status", None)            # a top-up void deletes nothing; the poller may move the status
    payload = {
        "user": user,
        "delete": resources["delete"],
        "purchases": [{k: v for k, v in p.items() if k != "used_bytes"} for p in resources["purchases"]],
        "connections": [{k: v for k, v in c.items() if k not in ("enabled", "node_name")}
                        for c in resources["connections"]],
        "prior_rights": (resources.get("prior_rights") or {}).get("cutover_snapshots"),
        "other_approvals": [(a["approval_uuid"], a["state"], int(a["version"])) for a in
                            other_approvals(db, approval, resources)],
        "effects": [(r["id"], r["effect_type"], r["effect_key"], r["resource_id"], r["voided_at"] is None) for r in rows],
        "sources": [(s["id"], int(s["version"]), s["state"]) for s in caused],
        "lots": lot_rows, "debts": debt_rows,
        "loyalty": [plan["purchase_events"], [(r["id"], r["required_active_count"]) for r in plan["rewards"]]],
        "card": None if card is None else (int(card["id"]), card["voided_at"] is None),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def state_hash(db, approval: dict, rows: list[dict], resources: dict, *, scope_fingerprint=None) -> str:
    digest = scope_fingerprint or fingerprint(db, approval, rows, resources)
    payload = [approval["approval_uuid"], int(approval["version"]), approval["state"], digest]
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()


def _sign(body: bytes) -> str:
    return hmac.new(settings.secret_key.encode(), b"receipt-void|" + body, hashlib.sha256).hexdigest()


def make_token(approval_uuid: str, digest: str, now: Optional[float] = None) -> str:
    body = json.dumps({"u": approval_uuid, "h": digest, "exp": int((now or time.time()) + TOKEN_SECONDS)},
                      sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + _sign(body)


def read_token(token: str, now: Optional[float] = None, *, check_expiry: bool = True) -> dict:
    """The signed body. check_expiry=False only to recognise the request
    that already created an operation (an exact replay is answered with that
    operation, never re-executed)."""
    try:
        encoded, signature = str(token).split(".", 1)
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(body)):
            raise ValueError("signature")
        data = json.loads(body)
        if not isinstance(data, dict) or not isinstance(data.get("u"), str) or not isinstance(data.get("h"), str):
            raise ValueError("shape")
    except (ValueError, TypeError):
        raise HTTPException(409, "preview_token_invalid") from None
    if check_expiry and int(data.get("exp", 0)) < int(now or time.time()):
        raise HTTPException(409, "preview_expired")
    return data


def preview(db, admin, *, approval_uuid=None, ledger_entry_id=None) -> dict:
    approval = find(db, admin, approval_uuid=approval_uuid, ledger_entry_id=ledger_entry_id)
    eligible(approval)
    rows = effects(db, approval["approval_uuid"])
    resources = scope(db, approval, rows)
    identity = resources["customer_identity_id"]
    plan = loyalty_plan(db, approval, resources)
    digest = state_hash(db, approval, rows, resources)
    return {
        "approval_uuid": approval["approval_uuid"], "kind": approval["kind"], "target_shape": approval["target_shape"],
        "username": approval["target_username_snapshot"], "telegram_id": approval["telegram_id_snapshot"],
        "amount": int(approval["amount_snapshot"]), "approval_mode": approval["approval_mode"],
        "approved_by_telegram_id": approval["approved_by_telegram_id"],
        "completed_at": approval["completed_at"],
        "resources": resources,
        "other_approvals_on_account": other_approvals(db, approval, resources) if resources["delete"] else [],
        "ledger": ledger(db, approval["approval_uuid"], rows),
        "wallet": wallet_projection(db, approval["approval_uuid"], identity, plan),
        "loyalty": plan,
        "effects": [{"id": r["id"], "effect_type": r["effect_type"], "effect_key": r["effect_key"],
                     "resource_type": r["resource_type"], "resource_id": r["resource_id"],
                     "delta_value": r["delta_value"], "reversal_state": r["reversal_state"]} for r in rows],
        "blockers": blockers(db, approval, rows, resources),
        "preview_token": make_token(approval["approval_uuid"], digest),
        "token_expires_in": TOKEN_SECONDS,
    }
