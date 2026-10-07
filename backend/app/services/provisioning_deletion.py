"""DB-only, fenced deletion T_final; no live executor or activation.

Persist snapshot() in operation.intent during deletion T1 before disabling.
Caller owns runtime/host validation, the fenced transaction and one commit.
No network, force, refund or hidden commit. Roll back every failure.
"""
import datetime as dt
import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import payment_reservations, provisioning_transitions as transitions
from . import resource_leases, user_ops, wallet_service
from .provisioning_identity import removal_matches


def _fingerprint(connection, node):
    values = [connection.id, connection.user_id, connection.purchase_id, connection.node_id,
              connection.type.value, connection.wg_peer_name, connection.wg_public_key,
              connection.wg_client_address, connection.ppp_username, connection.xr_email, connection.xr_uuid,
              node.type.value, node.mt_wireguard_interface, node.xr_panel_mode,
              node.xr_inbound_tag, node.xr_panel_inbound_id, node.mt_host, node.mt_port,
              node.mt_use_ssl, node.mt_api_ssl_port, node.xr_panel_base_url,
              node.xr_ssh_host, node.xr_ssh_port, node.se_host, node.se_port, node.se_hub_name]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def snapshot(resource_kind, resource_id, connections, *, purchase_ids=None):
    """T1 helper: typed IDs and one-way identity digests, no credentials."""
    if resource_kind not in ("connection", "purchase", "user") or type(resource_id) is not int or resource_id <= 0:
        raise HTTPException(422, "provisioning_deletion_scope_invalid")
    rows = list(connections)
    if len({row.id for row in rows}) != len(rows):
        raise HTTPException(422, "provisioning_deletion_scope_invalid")
    result = dict(resource_kind=resource_kind, resource_id=resource_id,
        connection_fingerprints={str(row.id): _fingerprint(row, row.node) for row in rows})
    if resource_kind == "user":
        if not isinstance(purchase_ids, (list, tuple)) or any(type(ident) is not int or ident <= 0 for ident in purchase_ids):
            raise HTTPException(422, "provisioning_deletion_scope_invalid")
        result["purchase_ids"] = sorted(set(purchase_ids))
    return json.dumps(result, sort_keys=True)


def finish(db, operation_id, version, leases):
    # Runtime read precedes resource rows and operation locks.
    wallet_service.require_legacy_phase(db)
    tokens = tuple(leases)
    if any(not isinstance(token, resource_leases.Lease) or not re.fullmatch(
            rf"op:{operation_id}:[1-9][0-9]*", token.owner) for token in tokens):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    if len({token.owner for token in tokens}) != 1:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    operation = payment_reservations._operation(db, operation_id)
    if operation.approval_uuid is not None:
        raise HTTPException(503, "provisioning_approval_integration_unavailable")
    if operation.operation_type not in ("delete_connection", "delete_purchase", "delete_user"):
        raise HTTPException(409, "provisioning_operation_transition_invalid")
    keys = {token.resource_key for token in tokens}
    if f"provisioning_op:{operation_id}" not in keys:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    if operation.state == "completed":
        return operation
    if operation.version != version or operation.state != "remote_complete":
        raise HTTPException(409, "provisioning_operation_changed")
    try:
        intent = json.loads(operation.intent)
        kind, ident, expected = intent["resource_kind"], intent["resource_id"], intent["connection_fingerprints"]
        if kind != operation.operation_type.removeprefix("delete_") or type(ident) is not int or ident <= 0 or not isinstance(expected, dict):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise HTTPException(409, "provisioning_deletion_scope_invalid") from None
    user = db.execute(select(models.User).where(models.User.id == operation.target_user_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if user is None:
        raise HTTPException(409, "target_user_missing")
    identity = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
        rv.wallet_accounts.c.user_id == user.id).with_for_update()).scalar_one_or_none()
    if identity is None or f"customer_identity:{identity}" not in keys:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    target = user if kind == "user" and ident == user.id else None
    if kind != "user":
        model = models.Connection if kind == "connection" else models.Purchase
        target = db.execute(select(model).where(model.id == ident, model.user_id == user.id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if target is None:
        raise HTTPException(409, "provisioning_deletion_scope_invalid")
    if kind == "user":
        purchase_ids = list(db.execute(select(models.Purchase.id).where(models.Purchase.user_id == user.id)
            .order_by(models.Purchase.id).with_for_update()).scalars())
        if intent.get("purchase_ids") != purchase_ids:
            raise HTTPException(409, "provisioning_deletion_scope_changed")
    query = select(models.Connection).where(models.Connection.user_id == user.id)
    if kind == "connection":
        query = query.where(models.Connection.id == ident)
    elif kind == "purchase":
        query = query.where(models.Connection.purchase_id == ident)
    connections = db.execute(query.order_by(models.Connection.id).with_for_update()
        .execution_options(populate_existing=True)).scalars().all()
    nodes = {node.id: node for node in db.execute(select(models.Node).where(
        models.Node.id.in_({row.node_id for row in connections})).order_by(models.Node.id)
        .with_for_update().execution_options(populate_existing=True)).scalars()}
    if any(row.node_id not in nodes for row in connections):
        raise HTTPException(409, "node_unavailable")
    actual = {str(row.id): _fingerprint(row, nodes[row.node_id]) for row in connections}
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation_id)
        .order_by(mp.ProvisioningStep.id).with_for_update().execution_options(populate_existing=True)).scalars().all()
    if actual != expected or len(steps) != len(connections) or {step.connection_id for step in steps} != {row.id for row in connections}:
        raise HTTPException(409, "provisioning_deletion_scope_changed")
    by_id = {row.id: row for row in connections}
    if any(step.direction != "remove" or step.state != "removed" or (
            step.node_id, step.protocol) != (by_id[step.connection_id].node_id, by_id[step.connection_id].type.value) or (
            not removal_matches(step, by_id[step.connection_id], nodes[step.node_id], removed=True)) or (
            step.backend != "radius_ppp" and (not step.remote_attempted or step.remote_outcome not in (
                "verified_absent", "delete_idempotently_absent")))
            for step in steps):
        raise HTTPException(409, "provisioning_steps_incomplete")
    if any(row.enabled for row in connections) or (kind == "user" and (
            user.status != models.UserStatus.disabled or not user.purchases_blocked)):
        raise HTTPException(409, "provisioning_deletion_not_disabled")
    if db.execute(select(mp.PaymentReservation.id).where(mp.PaymentReservation.operation_id == operation_id)).first():
        raise HTTPException(409, "provisioning_deletion_has_payment")
    if kind == "connection":
        user_ops.finalize_connection_deletion_after_deprovision(db, target)
    else:
        db.expire(target, ["connections"])
        if kind == "purchase":
            user_ops.finalize_purchase_deletion_after_deprovision(db, target)
        else:
            db.expire(target, ["purchases"])
            user_ops.finalize_user_deletion_after_deprovision(db, target)
    db.flush()
    transitions._write_operation(db, operation, "completed", username_claim=None,
        completed_at=dt.datetime.utcnow(), result_user_id=user.id)
    return operation
