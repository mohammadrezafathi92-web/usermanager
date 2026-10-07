"""Atomic non-approval T1 and one-transaction renewal. No live caller.

For a NEW operation its numeric ID does not exist before T1. Acquire the
canonical leases inside this short transaction after flushing that ID, then
revalidate before any claim, address or money mutation. LeaseBusy/any error
requires full rollback. No network, internal commit or partial prepare row.
The caller retains returned tokens across commit and renews/releases them.
"""
import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Literal

from fastapi import HTTPException
from pydantic import Field, StrictInt, StrictStr, validator
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import keys, payment_reservations, provisioning_claims, provisioning_contracts as contracts
from . import provisioning_finalization as final, provisioning_schema, provisioning_wireguard_pool
from . import receipt_void_schema, resource_leases, wallet_accounts, wallet_policy, wallet_service
from .marzneshin_client import sanitize_username
from .provisioning_host import HostIdentity, validate_snapshot


class Slot(final._Snapshot):
    slot_key: StrictStr
    node_id: StrictInt
    protocol: Literal["wireguard", "openvpn", "l2tp", "ikev2", "sstp", "pptp", "xray", "softether"]
    flow: StrictStr = ""
    max_concurrent_sessions: StrictInt = 1
    speed_limit_mbps: StrictInt | None = None

    @validator("max_concurrent_sessions", "speed_limit_mbps")
    def nonnegative_limit(cls, value):
        if value is not None and value < 0:
            raise ValueError("invalid limit")
        return value

    @validator("flow")
    def flow_length(cls, value):
        if len(value) > 64:
            raise ValueError("invalid flow")
        return value

    @validator("node_id")
    def positive(cls, value):
        if value <= 0:
            raise ValueError("invalid node")
        return value

    @validator("slot_key")
    def slot_length(cls, value):
        if not 1 <= len(value) <= 64:
            raise ValueError("invalid slot")
        return value


class Payer(final._Snapshot):
    kind: Literal["customer_wallet", "reseller_credit"]
    id: StrictInt
    amount: StrictInt

    @validator("id", "amount")
    def positive(cls, value):
        if value <= 0:
            raise ValueError("invalid payer")
        return value


class Preparation(final._Snapshot):
    operation_type: Literal["create_user", "purchase", "add_connection", "renew_user", "renew_purchase"]
    business_key: StrictStr
    tenant_scope_key: StrictStr
    target_user_id: StrictInt | None = None
    intent: final.FinalIntent
    slots: list[Slot] = Field(default_factory=list)
    payers: list[Payer] = Field(default_factory=list)

    @validator("target_user_id")
    def positive_target(cls, value):
        if value is not None and value <= 0:
            raise ValueError("invalid target")
        return value

    @validator("business_key")
    def business_length(cls, value):
        if not 1 <= len(value) <= 160:
            raise ValueError("invalid operation key")
        return value

    @validator("tenant_scope_key")
    def scope_length(cls, value):
        if not 1 <= len(value) <= 64:
            raise ValueError("invalid tenant scope")
        return value


@dataclass(repr=False)
class Prepared:
    operation: object = field(repr=False)
    leases: tuple = ()
    replay: bool = False


def _stage(db, operation, slot, node, backend):
    name = operation.username_claim or final._intent(operation).user.username
    attributes = dict(operation_id=operation.id, slot_key=slot.slot_key, direction="create",
        backend=backend, node_id=node.id, protocol=slot.protocol, flow=slot.flow, state="staged",
        max_concurrent_sessions=slot.max_concurrent_sessions, speed_limit_mbps=slot.speed_limit_mbps,
        xr_inbound_tag=node.xr_inbound_tag, xr_panel_inbound_id=node.xr_panel_inbound_id)
    if backend == "mikrotik_wg":
        private, public = keys.generate_wireguard_keypair()
        attributes.update(wg_interface=node.mt_wireguard_interface,
            wg_peer_name=f"user-{name}-{uuid.uuid4().hex[:6]}", staged_wg_private_key=private, wg_public_key=public)
    elif backend == "radius_ppp" or backend == "softether":
        suffix = "ovpn" if slot.protocol == "openvpn" else slot.protocol
        attributes.update(account_username=f"{name[:12]}-{suffix}{uuid.uuid4().hex[:8]}",
                          staged_password=keys.generate_password())
    else:
        # Both the email and Marzneshin's normalized account name are claimed
        # before any remote call, including unfinished steps on this node.
        used = set(db.execute(select(models.Connection.xr_email).where(models.Connection.node_id == node.id)).scalars())
        used.update(db.execute(select(mp.ProvisioningStep.xr_email).where(mp.ProvisioningStep.node_id == node.id,
            (mp.ProvisioningStep.state.notin_(("active", "removed"))) |
            (mp.ProvisioningStep.remote_outcome == "abandoned"))).scalars())
        normalized = {sanitize_username(value) for value in used if value} if backend == "marzneshin" else set()
        for _ in range(32):
            email = f"{name[:12]}{uuid.uuid4().hex[:4]}@usermanager.local"
            if email not in used and (backend != "marzneshin" or sanitize_username(email) not in normalized):
                break
        else:
            raise HTTPException(409, "provisioning_identity_exhausted")
        attributes.update(xr_email=email, staged_xr_uuid=str(uuid.uuid4()),
            account_username=sanitize_username(email) if backend == "marzneshin" else None)
    return attributes


def prepare(db, request, *, identity, ownership_epoch, installation_uuid, actor_kind="system", actor_id=None):
    if not isinstance(request, Preparation) or not isinstance(identity, HostIdentity):
        raise HTTPException(422, "provisioning_request_invalid")
    if actor_kind not in mp.ACTOR_KINDS or (actor_id is not None and (type(actor_id) is not int or actor_id <= 0)):
        raise HTTPException(422, "provisioning_request_invalid")
    if db.get_transaction() is None or db.info.get("resource_lease_business_transaction") is not db.get_transaction():
        raise resource_leases.LeaseProtocolError("business_transaction_not_fenced")
    provisioning_schema.assert_ready()
    receipt_void_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(rv.wallet_runtime_state.c.id == 1)
        .with_for_update(read=True)).scalar_one()
    db.execute(select(rv.receipt_approval_runtime_state.c.id).with_for_update(read=True)).all()
    mode = db.execute(select(mp.ProvisioningTypeMode.mode).where(
        mp.ProvisioningTypeMode.operation_type == request.operation_type).with_for_update(read=True)).scalar_one_or_none()
    if mode != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    runtime = db.execute(select(mp.ProvisioningRuntimeState.__table__).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update(read=True)).mappings().one()
    if runtime["owner_state"] == "draining":
        raise HTTPException(503, "provisioning_draining")
    validate_snapshot(runtime, identity, ownership_epoch, installation_uuid)
    if runtime["gate_mode"] != "enforced":
        raise HTTPException(503, "gate_not_enforced")
    panel = db.get(models.PanelSettings, 1)
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")
    canonical = json.dumps(request.dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    previous = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.operation_type == request.operation_type,
        mp.ProvisioningOperation.business_key == request.business_key).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if previous:
        if previous.request_hash != digest or previous.tenant_scope_key != request.tenant_scope_key:
            raise HTTPException(409, "provisioning_request_changed")
        return Prepared(previous, replay=True)
    renewing = request.operation_type.startswith("renew_")
    if len({slot.slot_key for slot in request.slots}) != len(request.slots) or (
            renewing and request.slots) or (request.operation_type == "add_connection" and (
            len(request.slots) != 1 or request.payers)) or len({payer.kind for payer in request.payers}) != len(request.payers):
        raise HTTPException(422, "provisioning_request_invalid")
    intent = request.intent.copy(deep=True)
    kind = request.operation_type
    if (intent.purchase_id is not None and kind not in ("add_connection", "renew_purchase")) or (
            kind == "renew_purchase" and intent.purchase_id is None) or (
            kind == "purchase" and intent.package is None) or (
            kind == "create_user" and intent.package and (intent.quota_bytes or intent.duration_days)) or (
            kind == "add_connection" and (intent.package or intent.sale)) or (
            kind != "create_user" and request.target_user_id is None):
        raise HTTPException(422, "provisioning_request_invalid")
    for payer in request.payers:
        if intent.sale is None or (payer.kind == "customer_wallet" and (
                kind == "create_user" or payer.id != request.target_user_id or
                intent.sale.payment_method != "wallet" or payer.amount != intent.sale.amount)):
            raise HTTPException(422, "provisioning_payment_invalid")
    if intent.sale and intent.sale.payment_method == "wallet" and intent.sale.amount > 0 and not any(
            payer.kind == "customer_wallet" for payer in request.payers):
        raise HTTPException(422, "provisioning_payment_invalid")
    nodes = {}
    for slot in request.slots:
        node = db.execute(select(models.Node).where(models.Node.id == slot.node_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if node is None or not node.enabled:
            raise HTTPException(409, "node_unavailable")
        backend = contracts.backend_for(node, slot.protocol)
        nodes[slot.slot_key] = (node, backend)
    intent.node_fingerprints = {str(node.id): contracts.config_fingerprint(node) for node, backend in nodes.values()}
    if intent.package and db.get(models.Package, intent.package.id) is None:
        raise HTTPException(409, "provisioning_package_missing")
    if request.operation_type == "create_user":
        if request.target_user_id is not None or wallet_accounts.tenant_scope(db, intent.user.owner_admin_id) != request.tenant_scope_key:
            raise HTTPException(409, "provisioning_target_changed")
        if db.query(models.User.id).filter_by(username=intent.user.username).first():
            raise HTTPException(409, "provisioning_username_taken")
        wallet_policy.can_purchase_telegram(db, intent.user.telegram_id)
        customer = db.execute(select(rv.customer_identities.c.id).where(
            rv.customer_identities.c.tenant_scope_key == request.tenant_scope_key,
            rv.customer_identities.c.identity_key == f"tg:{intent.user.telegram_id}")).scalar_one_or_none()
    else:
        user = db.execute(select(models.User).where(models.User.id == request.target_user_id)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if user is None or (user.username, user.telegram_id, user.owner_admin_id) != (
                intent.user.username, intent.user.telegram_id, intent.user.owner_admin_id) or (
                wallet_accounts.tenant_scope(db, user.owner_admin_id) != request.tenant_scope_key):
            raise HTTPException(409, "provisioning_target_changed")
        wallet_policy.can_purchase(db, user)
        customer = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
            rv.wallet_accounts.c.user_id == user.id, rv.wallet_accounts.c.tombstoned_at.is_(None))).scalar_one()
    operation = mp.ProvisioningOperation(operation_type=request.operation_type, business_key=request.business_key,
        request_hash=digest, tenant_scope_key=request.tenant_scope_key, actor_kind=actor_kind, actor_id=actor_id,
        intent=intent.json(), target_user_id=request.target_user_id, wallet_epoch_at_start=epoch, state="prepared",
        username_claim=intent.user.username if request.operation_type == "create_user" else None,
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
    db.add(operation)
    db.flush()
    resource_keys = {f"provisioning_op:{operation.id}"}
    if customer is not None:
        resource_keys.add(f"customer_identity:{customer}")
    resource_keys.update(f"node:{node.id}:wg_pool" for node, backend in nodes.values() if backend == "mikrotik_wg")
    tokens = tuple(resource_leases.acquire(db, key, f"op:{operation.id}:1") for key in sorted(resource_keys))
    resource_leases.revalidate(db, tokens)
    # Preflight reads above do not lock target rows before resource leases.
    # Re-read targets after fencing, before any benefit, claim or payment.
    if request.target_user_id is not None:
        fresh = final._locked(db, models.User, request.target_user_id)
        current_customer = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
            rv.wallet_accounts.c.user_id == request.target_user_id,
            rv.wallet_accounts.c.tombstoned_at.is_(None))).scalar_one_or_none()
        if fresh is None or current_customer != customer or (fresh.username, fresh.telegram_id, fresh.owner_admin_id) != (
                intent.user.username, intent.user.telegram_id, intent.user.owner_admin_id):
            raise HTTPException(409, "provisioning_target_changed")
        wallet_policy.can_purchase(db, fresh)
        if intent.purchase_id is not None:
            purchase = final._locked(db, models.Purchase, intent.purchase_id)
            if purchase is None or purchase.user_id != fresh.id:
                raise HTTPException(409, "provisioning_target_changed")
    if intent.package:
        live_package = final._locked(db, models.Package, intent.package.id)
        if live_package is None or (live_package.name, int(round((live_package.quota_gb or 0) * 1024 ** 3)),
                live_package.duration_days or 0, live_package.max_concurrent_sessions) != (
                intent.package.name, intent.package.quota_bytes, intent.package.duration_days,
                intent.package.max_concurrent_sessions):
            raise HTTPException(409, "provisioning_package_changed")
    if intent.package and request.operation_type in ("create_user", "purchase"):
        provisioning_claims.reserve(db, operation.id, intent.package.id, telegram_id=intent.user.telegram_id)
    for slot_key, (node, backend) in sorted(nodes.items(), key=lambda entry: (entry[1][0].id, entry[0])):
        fresh = final._locked(db, models.Node, node.id)
        if fresh is None or not fresh.enabled or contracts.config_fingerprint(fresh) != intent.node_fingerprints[str(node.id)]:
            raise HTTPException(409, "provisioning_node_config_changed")
        contracts.require_ready(db, fresh, backend)
        nodes[slot_key] = (fresh, backend)
    for order, slot in enumerate(request.slots):
        node, backend = nodes[slot.slot_key]
        step = mp.ProvisioningStep(step_order=order, **_stage(db, operation, slot, node, backend))
        db.add(step)
        db.flush()
        if backend == "mikrotik_wg":
            provisioning_wireguard_pool.allocate(db, step.id, step.version, tokens,
                count=max(1, slot.max_concurrent_sessions or 1))
    for payer in request.payers:
        payment_reservations.reserve(db, operation.id, payer.kind, payer.id, payer.amount)
    if renewing:
        final.finish(db, operation.id, operation.version, tokens)
    db.flush()
    return Prepared(operation, tokens)
