"""Private DB-only deletion T1 (Lifecycle 12); no API/executor/activation.

The trusted caller authorizes the target before entering, owns a fenced
writer transaction, rolls back any failure and commits exactly once. This
only stages immutable removal identity and disables the selected scope;
it NEVER deletes records, refunds money, contacts a node or compensates.
The caller retains/releases the returned canonical resource leases.
"""
import datetime as dt
import hashlib
import json
from dataclasses import dataclass

from fastapi import HTTPException
from pydantic import StrictInt, StrictStr, validator
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import provisioning_deletion as deletion
from . import provisioning_contracts as contracts, provisioning_schema, receipt_void_schema
from . import resource_leases, wallet_accounts, wallet_service
from .provisioning_host import HostIdentity, validate_snapshot
from .provisioning_finalization import _Snapshot


class DeleteRequest(_Snapshot):
    resource_kind: StrictStr
    resource_id: StrictInt
    business_key: StrictStr
    tenant_scope_key: StrictStr

    @validator("resource_kind")
    def kind(cls, value):
        if value not in ("user", "purchase", "connection"):
            raise ValueError("invalid deletion kind")
        return value

    @validator("resource_id")
    def positive(cls, value):
        if value <= 0:
            raise ValueError("invalid deletion target")
        return value

    @validator("business_key")
    def business_length(cls, value):
        if not 1 <= len(value) <= 160:
            raise ValueError("invalid deletion key")
        return value

    @validator("tenant_scope_key")
    def scope_length(cls, value):
        if not 1 <= len(value) <= 64:
            raise ValueError("invalid deletion scope")
        return value


def _scope(db, request, *, lock, user_id=None):
    model = {"user": models.User, "purchase": models.Purchase, "connection": models.Connection}[request.resource_kind]
    def row(query):
        if lock:
            query = query.with_for_update()
        return db.execute(query.execution_options(populate_existing=True)).scalar_one_or_none()
    if lock:
        # L4 always locks User before its Purchase/Connection targets.
        user = row(select(models.User).where(models.User.id == user_id))
        query = select(model).where(model.id == request.resource_id)
        if request.resource_kind != "user":
            query = query.where(model.user_id == user_id)
        target = user if request.resource_kind == "user" and request.resource_id == user_id else row(query)
    else:
        target = row(select(model).where(model.id == request.resource_id))
        user = target if request.resource_kind == "user" else (
            row(select(models.User).where(models.User.id == target.user_id)) if target is not None else None)
    if target is None:
        raise HTTPException(404, "provisioning_deletion_target_missing")
    if user is None or wallet_accounts.tenant_scope(db, user.owner_admin_id) != request.tenant_scope_key:
        raise HTTPException(409, "provisioning_deletion_scope_changed")
    query = select(models.Connection).where(models.Connection.user_id == user.id)
    if request.resource_kind == "connection":
        query = query.where(models.Connection.id == target.id)
    elif request.resource_kind == "purchase":
        query = query.where(models.Connection.purchase_id == target.id)
    if lock:
        query = query.with_for_update()
    connections = db.execute(query.order_by(models.Connection.id)
        .execution_options(populate_existing=True)).scalars().all()
    purchases = select(models.Purchase.id).where(models.Purchase.user_id == user.id).order_by(models.Purchase.id)
    if lock:
        purchases = purchases.with_for_update()
    purchase_ids = tuple(db.execute(purchases).scalars()) if request.resource_kind == "user" else ()
    identity = db.scalar(select(rv.wallet_accounts.c.customer_identity_id).where(
        rv.wallet_accounts.c.user_id == user.id, rv.wallet_accounts.c.tombstoned_at.is_(None)))
    if identity is None:
        raise HTTPException(409, "provisioning_deletion_wallet_missing")
    return user, connections, purchase_ids, identity


@dataclass(repr=False)
class PreparedDeletion:
    operation: object
    leases: tuple = ()
    replay: bool = False


def prepare(db, request, *, identity, ownership_epoch, installation_uuid, actor_kind="system", actor_id=None):
    if not isinstance(request, DeleteRequest) or not isinstance(identity, HostIdentity) or (
            actor_kind not in mp.ACTOR_KINDS or actor_id is not None and (type(actor_id) is not int or actor_id <= 0)):
        raise HTTPException(422, "provisioning_request_invalid")
    if db.get_transaction() is None or db.info.get("resource_lease_business_transaction") is not db.get_transaction():
        raise resource_leases.LeaseProtocolError("business_transaction_not_fenced")
    provisioning_schema.assert_ready()
    receipt_void_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    epoch = db.scalar(select(rv.wallet_runtime_state.c.epoch).where(rv.wallet_runtime_state.c.id == 1)
        .with_for_update(read=True))
    db.execute(select(rv.receipt_approval_runtime_state.c.id).with_for_update(read=True)).all()
    kind = "delete_" + request.resource_kind
    if db.scalar(select(mp.ProvisioningTypeMode.mode).where(mp.ProvisioningTypeMode.operation_type == kind)
            .with_for_update(read=True)) != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    runtime = db.execute(select(mp.ProvisioningRuntimeState.__table__).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update(read=True)).mappings().one()
    if runtime["owner_state"] == "draining":
        raise HTTPException(503, "provisioning_draining")
    validate_snapshot(runtime, identity, ownership_epoch, installation_uuid)
    if runtime["lock_backend"] != ("flock" if db.get_bind().dialect.name == "sqlite" else "flock+get_lock"):
        raise HTTPException(503, "ownership_lock_not_ready")
    if runtime["gate_mode"] != "enforced":
        raise HTTPException(503, "gate_not_enforced")
    panel = db.get(models.PanelSettings, 1)
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")
    digest = hashlib.sha256(json.dumps(request.dict(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    previous = db.execute(select(mp.ProvisioningOperation).where(mp.ProvisioningOperation.operation_type == kind,
        mp.ProvisioningOperation.business_key == request.business_key).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if previous is not None:
        if previous.request_hash != digest or previous.tenant_scope_key != request.tenant_scope_key:
            raise HTTPException(409, "provisioning_request_changed")
        return PreparedDeletion(previous, replay=True)
    user, connections, purchases, customer = _scope(db, request, lock=False)
    uid, selected = user.id, tuple(row.id for row in connections)
    operation = mp.ProvisioningOperation(operation_type=kind, business_key=request.business_key, request_hash=digest,
        tenant_scope_key=request.tenant_scope_key, actor_kind=actor_kind, actor_id=actor_id, target_user_id=uid,
        intent="{}", state="prepared", wallet_epoch_at_start=epoch,
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
    db.add(operation)
    db.flush()
    tokens = tuple(resource_leases.acquire(db, key, f"op:{operation.id}:1") for key in sorted((
        f"customer_identity:{customer}", f"provisioning_op:{operation.id}")))
    resource_leases.revalidate(db, tokens)
    user, connections, fresh_purchases, fresh_customer = _scope(db, request, lock=True, user_id=uid)
    if (user.id, tuple(row.id for row in connections), fresh_purchases, fresh_customer) != (uid, selected, purchases, customer):
        raise HTTPException(409, "provisioning_deletion_scope_changed")
    nodes = {}
    for node_id in sorted({row.node_id for row in connections}):
        node = db.execute(select(models.Node).where(models.Node.id == node_id).with_for_update()
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if node is None:
            raise HTTPException(409, "node_unavailable")
        nodes[node_id] = node
    for row in connections:
        # snapshot() uses the CURRENT locked node, not an ORM relationship
        # cached by an earlier caller read. Disabled nodes still need cleanup.
        row.node = nodes[row.node_id]
    operation.intent = deletion.snapshot(request.resource_kind, request.resource_id, connections, purchase_ids=purchases)
    for order, row in enumerate(connections):
        node = nodes[row.node_id]
        backend = contracts.backend_for(node, row.type.value)
        contracts.require_ready(db, node, backend)
        step = mp.ProvisioningStep(operation_id=operation.id, slot_key=f"connection:{row.id}", step_order=order,
            connection_id=row.id, node_id=row.node_id, protocol=row.type.value, backend=backend,
            direction="remove", state="staged", remote_attempted=False,
            wg_interface=node.mt_wireguard_interface if backend == "mikrotik_wg" else None,
            wg_peer_name=row.wg_peer_name if backend == "mikrotik_wg" else None,
            wg_public_key=row.wg_public_key if backend == "mikrotik_wg" else None,
            wg_client_address=row.wg_client_address if backend == "mikrotik_wg" else None,
            account_username=row.ppp_username if backend in ("radius_ppp", "softether") else None,
            xr_email=row.xr_email if backend in contracts.XRAY_MODES else None,
            xr_inbound_tag=node.xr_inbound_tag if backend in contracts.XRAY_MODES else None,
            xr_panel_inbound_id=node.xr_panel_inbound_id if backend in contracts.XRAY_MODES else None,
            # UUID is Xray removal identity, not a delivered/private key.
            staged_xr_uuid=row.xr_uuid if backend in contracts.XRAY_MODES else None)
        if not deletion.removal_matches(step, row, node):
            raise HTTPException(409, "provisioning_identity_incomplete")
        db.add(step)
        row.enabled = False
    if request.resource_kind == "user":
        user.status, user.purchases_blocked = models.UserStatus.disabled, True
    db.flush()
    return PreparedDeletion(operation, tokens)
