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
    out = {"shape": shape, "user_id": user_id, "user_exists": user is not None, "delete": None,
           "purchases": [], "connections": []}
    if shape == "topup" or user is None:
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
    out["purchases"] = [{"id": p.id, "package_id": p.package_id, "comment": p.comment} for p in purchases]
    out["connections"] = [{"id": c.id, "purchase_id": c.purchase_id, "protocol": getattr(c.type, "value", c.type),
                           "node_id": c.node_id, "node_name": nodes.get(c.node_id), "enabled": bool(c.enabled)}
                          for c in connections]
    return out


def other_approvals(db, approval: dict, user_id: Optional[int]) -> list[dict]:
    if user_id is None:
        return []
    approvals, effects_table = rv.receipt_approvals, rv.receipt_approval_effects
    created_here = select(effects_table.c.approval_uuid).where(
        effects_table.c.effect_type == "user_created", effects_table.c.resource_id == user_id)
    rows = db.execute(select(approvals.c.approval_uuid, approvals.c.kind, approvals.c.state,
                             approvals.c.amount_snapshot).where(
        approvals.c.approval_uuid != approval["approval_uuid"],
        (approvals.c.target_user_id_snapshot == user_id) | approvals.c.approval_uuid.in_(created_here))
        .order_by(approvals.c.id)).mappings().all()
    return [dict(row) for row in rows]


def customer_identity(db, user_id: Optional[int]) -> Optional[int]:
    if user_id is None:
        return None
    return db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
        rv.wallet_accounts.c.user_id == user_id)).scalar()


# ------------------------------------------------------------------ money (12.6 projections, 14)
def sources(db, approval_uuid: str) -> list[dict]:
    table = rv.wallet_credit_sources
    return [dict(row) for row in db.execute(select(table).where(
        table.c.causation_approval_uuid == approval_uuid).order_by(table.c.id)).mappings()]


def wallet_projection(db, approval_uuid: str, identity_id: Optional[int]) -> dict:
    lots, apps, allocations = rv.wallet_lots, rv.wallet_credit_applications, rv.wallet_allocations
    items = []
    for source in sources(db, approval_uuid):
        source_lots = [dict(r) for r in db.execute(select(lots).where(
            lots.c.source_credit_source_id == source["id"]).order_by(lots.c.id)).mappings()]
        settlements = [dict(r) for r in db.execute(select(apps).where(
            apps.c.wallet_credit_source_id == source["id"], apps.c.application_kind == "debt_settlement")).mappings()]
        clawback = debt = writeoff = 0
        spent_by = []
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
            spent_by += [int(r.wallet_debit_event_id) for r in db.execute(select(allocations.c.wallet_debit_event_id)
                                                                          .where(allocations.c.wallet_lot_id == lot["id"]))]
        items.append({"source_id": source["id"], "origin_kind": source["origin_kind"], "state": source["state"],
                      "gross": int(source["gross_amount"]),
                      "projected_clawback": clawback, "projected_debt": debt, "projected_writeoff": writeoff,
                      "projected_settlement_reopen": sum(int(a["amount"]) - int(a["reversed_amount"]) for a in settlements),
                      "spent_by_debits": sorted(set(spent_by)), "spent_sales_stay_valid": True})
    return {"sources": items, "open_debt": wallet_lots.open_debt(db, identity_id) if identity_id else 0}


def ledger(db, approval_uuid: str, rows: list[dict]) -> dict:
    entries = db.query(models.LedgerEntry).filter(models.LedgerEntry.approval_uuid == approval_uuid).order_by(
        models.LedgerEntry.id).all()
    spend = _effect(rows, "ledger_credit_spend", "credit_spend")
    spend_entry = db.get(models.LedgerEntry, int(spend["resource_id"])) if spend and spend["resource_id"] else None
    discount = _effect(rows, "discount_redeemed")
    card = db.execute(select(payment_card_events._tables()[2]).where(
        payment_card_events._tables()[2].c.approval_uuid == approval_uuid)).mappings().first()
    return {
        "entries": [{"id": e.id, "kind": e.kind, "amount": int(e.amount or 0), "voided": e.voided_at is not None}
                    for e in entries if e.kind in ("sale_new", "sale_renew", "wallet_topup")],
        "voidable_sale_amount": sum(int(e.amount or 0) for e in entries
                                    if e.kind in ("sale_new", "sale_renew") and e.voided_at is None),
        "reseller_credit_refund": ({"ledger_entry_id": spend_entry.id, "admin_id": spend_entry.admin_id,
                                    "amount": int(spend_entry.amount or 0)} if spend_entry else None),
        "discount": ({"redemption_id": discount["resource_id"], "amount": discount["delta_value"]}
                     if discount else None),
        "card": ({"event_id": card["id"], "card_id": card["card_id_snapshot"], "amount": int(card["amount"]),
                  "voided": card["voided_at"] is not None} if card else None),
    }


def loyalty(db, approval_uuid: str) -> dict:
    events, rewards = rv.loyalty_purchase_events, rv.loyalty_reward_events
    return {"purchase_events": [int(r.id) for r in db.execute(select(events.c.id).where(
                events.c.approval_uuid == approval_uuid, events.c.state == "active"))],
            "rewards_caused": [int(r.id) for r in db.execute(select(rewards.c.id).where(
                rewards.c.causation_approval_uuid == approval_uuid, rewards.c.state == "active"))]}


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
    if resources["user_exists"] and customer_identity(db, resources["user_id"]) is None:
        out.append("wallet_account_not_ready")
    return out


def preflight(db, approval: dict, rows: list[dict], resources: dict) -> None:
    found = blockers(db, approval, rows, resources)
    if found:
        raise HTTPException(409, found[0])


# ------------------------------------------------------------------ state hash and token (18)
def state_hash(db, approval: dict, rows: list[dict], resources: dict) -> str:
    lots = rv.wallet_lots
    source_rows = sources(db, approval["approval_uuid"])
    lot_rows = [] if not source_rows else [
        (int(r.id), int(r.version), r.state) for r in db.execute(select(lots.c.id, lots.c.version, lots.c.state).where(
            lots.c.source_credit_source_id.in_([s["id"] for s in source_rows])).order_by(lots.c.id))]
    payload = {
        "approval": [approval["approval_uuid"], int(approval["version"]), approval["state"]],
        "sources": [(s["id"], int(s["version"]), s["state"]) for s in source_rows],
        "lots": lot_rows,
        "effects": [(r["id"], r["voided_at"] is None) for r in rows],
        "connections": [c["id"] for c in resources["connections"]],
        "purchases": [p["id"] for p in resources["purchases"]],
        "user": resources["user_id"],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _sign(body: bytes) -> str:
    return hmac.new(settings.secret_key.encode(), b"receipt-void|" + body, hashlib.sha256).hexdigest()


def make_token(approval_uuid: str, digest: str, now: Optional[float] = None) -> str:
    body = json.dumps({"u": approval_uuid, "h": digest, "exp": int((now or time.time()) + TOKEN_SECONDS)},
                      sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode().rstrip("=") + "." + _sign(body)


def read_token(token: str, now: Optional[float] = None) -> dict:
    try:
        encoded, signature = str(token).split(".", 1)
        body = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(body)):
            raise ValueError("signature")
        data = json.loads(body)
    except (ValueError, TypeError):
        raise HTTPException(409, "preview_token_invalid") from None
    if int(data.get("exp", 0)) < int(now or time.time()):
        raise HTTPException(409, "preview_expired")
    return data


def preview(db, admin, *, approval_uuid=None, ledger_entry_id=None) -> dict:
    approval = find(db, admin, approval_uuid=approval_uuid, ledger_entry_id=ledger_entry_id)
    eligible(approval)
    rows = effects(db, approval["approval_uuid"])
    resources = scope(db, approval, rows)
    identity = customer_identity(db, resources["user_id"])
    digest = state_hash(db, approval, rows, resources)
    return {
        "approval_uuid": approval["approval_uuid"], "kind": approval["kind"], "target_shape": approval["target_shape"],
        "username": approval["target_username_snapshot"], "telegram_id": approval["telegram_id_snapshot"],
        "amount": int(approval["amount_snapshot"]), "approval_mode": approval["approval_mode"],
        "approved_by_telegram_id": approval["approved_by_telegram_id"],
        "completed_at": approval["completed_at"],
        "resources": resources,
        "other_approvals_on_account": other_approvals(db, approval, resources["user_id"])
        if approval["target_shape"] in FULL_ACCOUNT else [],
        "ledger": ledger(db, approval["approval_uuid"], rows),
        "wallet": wallet_projection(db, approval["approval_uuid"], identity),
        "loyalty": loyalty(db, approval["approval_uuid"]),
        "effects": [{"effect_type": r["effect_type"], "effect_key": r["effect_key"], "delta_value": r["delta_value"],
                     "reversal_state": r["reversal_state"]} for r in rows],
        "blockers": blockers(db, approval, rows, resources),
        "preview_token": make_token(approval["approval_uuid"], digest),
        "token_expires_in": TOKEN_SECONDS,
    }
