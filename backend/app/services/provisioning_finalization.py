"""One DB-only T_final for non-approval provisioning; no live activation.

Caller validates host/contracts, owns canonical leases and begin_business(),
rolls back any failure, then commits once. Renewal reconciliation is returned
as IDs and happens only AFTER commit. Approval operations remain fail-closed.
"""
import datetime as dt
import json
import re

from fastapi import HTTPException
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr, ValidationError, validator
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import accounting, payment_reservations, provisioning_claims, provisioning_records
from . import provisioning_contracts
from . import provisioning_transitions as transitions, receipt_void_schema, resource_leases
from . import user_ops, wallet_accounts, wallet_policy, wallet_service


class _Snapshot(BaseModel):
    class Config:
        extra = "forbid"


class UserSnapshot(_Snapshot):
    username: StrictStr
    full_name: StrictStr | None = None
    notes: StrictStr | None = None
    telegram_id: StrictInt | None = None
    owner_admin_id: StrictInt | None = None

    @validator("username")
    def username_length(cls, value):
        if not 1 <= len(value) <= 64:
            raise ValueError("invalid username")
        return value

    @validator("telegram_id", "owner_admin_id")
    def positive_identity(cls, value):
        if value is not None and value <= 0:
            raise ValueError("invalid identity")
        return value


class PackageSnapshot(_Snapshot):
    id: StrictInt
    name: StrictStr
    quota_bytes: StrictInt
    duration_days: StrictInt
    max_concurrent_sessions: StrictInt | None = None

    @validator("id")
    def positive(cls, value):
        if value <= 0:
            raise ValueError("invalid package")
        return value

    @validator("quota_bytes", "duration_days")
    def nonnegative(cls, value):
        if value < 0:
            raise ValueError("invalid package limits")
        return value


class SaleSnapshot(_Snapshot):
    amount: StrictInt
    admin_id: StrictInt | None = None
    actor_admin_id: StrictInt | None = None
    payment_method: StrictStr | None = None
    payment_card_id: StrictInt | None = None

    @validator("amount")
    def nonnegative(cls, value):
        if value < 0:
            raise ValueError("invalid sale")
        return value


class FinalIntent(_Snapshot):
    # Persisted by T1; never reconstructed from live package prices/limits.
    schema_version: StrictInt = 1
    user: UserSnapshot
    package: PackageSnapshot | None = None
    sale: SaleSnapshot | None = None
    purchase_id: StrictInt | None = None
    purchase_batch: StrictStr | None = None
    comment: StrictStr | None = None
    quota_bytes: StrictInt = 0
    duration_days: StrictInt = 0
    reset_usage: StrictBool = False
    node_fingerprints: dict[str, StrictStr] | None = None

    @validator("node_fingerprints")
    def fingerprints(cls, value):
        if value is not None and any(not re.fullmatch(r"[1-9][0-9]*", key) or
                not re.fullmatch(r"[0-9a-f]{64}", digest) for key, digest in value.items()):
            raise ValueError("invalid node fingerprint")
        return value

    @validator("purchase_batch")
    def batch_length(cls, value):
        if value is not None and not 1 <= len(value) <= 40:
            raise ValueError("invalid batch")
        return value

    @validator("schema_version")
    def supported(cls, value):
        if value != 1:
            raise ValueError("unsupported intent")
        return value

    @validator("quota_bytes", "duration_days")
    def nonnegative(cls, value):
        if value < 0:
            raise ValueError("invalid limits")
        return value


def _intent(operation):
    try:
        return FinalIntent.parse_obj(json.loads(operation.intent))
    except (ValueError, TypeError, ValidationError):
        raise HTTPException(409, "provisioning_intent_invalid") from None


def _locked(db, model, ident):
    return db.execute(select(model).where(model.id == ident).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()


def finish(db, operation_id, version, leases):
    """Atomically build/capture/complete; replay never repeats a mutation."""
    receipt_void_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).scalar_one()
    tokens = tuple(leases)
    resource_leases.revalidate(db, tokens)
    operation = transitions._operation(db, operation_id)
    keys = {token.resource_key for token in tokens}
    if f"provisioning_op:{operation.id}" not in keys or len({token.owner for token in tokens}) != 1 or any(
            not token.owner.startswith(f"op:{operation.id}:") for token in tokens):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    if operation.wallet_epoch_at_start != epoch:
        raise HTTPException(409, "wallet_epoch_changed")
    if operation.state == "completed":
        return operation
    renewal = operation.operation_type in ("renew_user", "renew_purchase")
    if operation.operation_type not in ("create_user", "purchase", "add_connection", "renew_user", "renew_purchase") or (
            operation.version != version or operation.state != ("prepared" if renewal else "remote_complete")):
        raise HTTPException(409, "provisioning_operation_changed")
    intent = _intent(operation)
    if intent.purchase_id is not None and operation.operation_type not in ("add_connection", "renew_purchase") or (
            operation.operation_type == "create_user" and intent.package and (intent.quota_bytes or intent.duration_days)):
        raise HTTPException(409, "provisioning_intent_invalid")
    reservations = db.execute(select(mp.PaymentReservation).where(mp.PaymentReservation.operation_id == operation.id)
        .order_by(mp.PaymentReservation.payer_kind).with_for_update()
        .execution_options(populate_existing=True)).scalars().all()
    if any(row.state != "reserved" for row in reservations) or (reservations and not intent.sale) or (
            any(row.payer_kind == "customer_wallet" for row in reservations) and intent.sale.payment_method != "wallet"):
        raise HTTPException(409, "provisioning_reservation_invalid")
    steps = db.execute(select(mp.ProvisioningStep).where(mp.ProvisioningStep.operation_id == operation.id)
        .order_by(mp.ProvisioningStep.step_order).with_for_update()
        .execution_options(populate_existing=True)).scalars().all()
    if renewal and steps or any(step.direction != "create" or step.state != (
            "staged" if step.backend == "radius_ppp" else "remote_created") for step in steps):
        raise HTTPException(409, "provisioning_steps_incomplete")
    if operation.operation_type == "add_connection" and (len(steps) != 1 or intent.sale or intent.package):
        raise HTTPException(409, "provisioning_intent_invalid")
    package = (models.Package(id=intent.package.id, name=intent.package.name,
        quota_gb=intent.package.quota_bytes / 1024 ** 3, duration_days=intent.package.duration_days,
        max_concurrent_sessions=intent.package.max_concurrent_sessions) if intent.package else None)
    if operation.operation_type == "purchase" and package is None:
        raise HTTPException(409, "provisioning_intent_invalid")
    if package is not None and db.get(models.Package, package.id) is None:
        raise HTTPException(409, "provisioning_package_missing")
    if operation.operation_type == "create_user":
        if operation.target_user_id is not None or operation.username_claim != intent.user.username or (
                wallet_accounts.tenant_scope(db, intent.user.owner_admin_id) != operation.tenant_scope_key):
            raise HTTPException(409, "provisioning_target_changed")
        if intent.user.telegram_id is not None:
            identity = db.execute(select(rv.customer_identities.c.id).where(
                rv.customer_identities.c.tenant_scope_key == operation.tenant_scope_key,
                rv.customer_identities.c.identity_key == f"tg:{intent.user.telegram_id}")).scalar_one_or_none()
            if identity is not None and f"customer_identity:{identity}" not in keys:
                raise HTTPException(409, "provisioning_lease_scope_invalid")
        wallet_policy.can_purchase_telegram(db, intent.user.telegram_id)
        user = user_ops.create_user_record_core(db, **intent.user.dict(), quota_gb=intent.quota_bytes / 1024 ** 3,
            expire_days=intent.duration_days, package_id=package.id if package else None, defer_loyalty=True)
        user.total_quota_bytes = intent.quota_bytes
    else:
        user = _locked(db, models.User, operation.target_user_id)
        if user is None or (user.username, user.telegram_id, user.owner_admin_id) != (
                intent.user.username, intent.user.telegram_id, intent.user.owner_admin_id) or (
                wallet_accounts.tenant_scope(db, user.owner_admin_id) != operation.tenant_scope_key):
            raise HTTPException(409, "provisioning_target_changed")
        identity = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
            rv.wallet_accounts.c.user_id == user.id, rv.wallet_accounts.c.tombstoned_at.is_(None))).scalar_one_or_none()
        if identity is None or f"customer_identity:{identity}" not in keys:
            raise HTTPException(409, "provisioning_lease_scope_invalid")
        if operation.operation_type != "add_connection":
            wallet_policy.can_purchase(db, user)
    if package is not None and _locked(db, models.Package, package.id) is None:
        raise HTTPException(409, "provisioning_package_missing")
    purchase = None
    reconcile = []
    if operation.operation_type in ("create_user", "purchase") and package:
        purchase = user_ops.build_purchase_core(db, user, package, intent.comment,
            count_purchase=operation.operation_type != "create_user", grant_loyalty=False)
        purchase.quota_bytes = intent.package.quota_bytes
        provisioning_claims.bind_purchase(db, operation.id, purchase.id)
    elif intent.purchase_id is not None:
        purchase = _locked(db, models.Purchase, intent.purchase_id)
        if purchase is None or purchase.user_id != user.id:
            raise HTTPException(409, "provisioning_target_changed")
    if operation.operation_type == "renew_purchase" and purchase is None:
        raise HTTPException(409, "provisioning_intent_invalid")
    if renewal:
        core = user_ops.renew_purchase_core if purchase is not None else user_ops.renew_user_core
        core(db, purchase if purchase is not None else user, add_gb=intent.quota_bytes / 1024 ** 3,
            add_days=intent.duration_days, reset_usage=intent.reset_usage,
            package_id=package.id if package else None, reconciliation_sink=reconcile)
    if intent.node_fingerprints is not None:
        if set(intent.node_fingerprints) != {str(step.node_id) for step in steps}:
            raise HTTPException(409, "provisioning_node_config_changed")
        for node_id, fingerprint in sorted(intent.node_fingerprints.items(), key=lambda item: int(item[0])):
            node = _locked(db, models.Node, int(node_id))
            if node is None or provisioning_contracts.config_fingerprint(node) != fingerprint:
                raise HTTPException(409, "provisioning_node_config_changed")
    for step in steps:
        provisioning_records.build_connection_core(db, step.id, step.version, user_id=user.id,
            purchase_id=purchase.id if purchase else None, purchase_batch=intent.purchase_batch)
    if operation.operation_type in ("create_user", "purchase"):
        user_ops._maybe_grant_loyalty_reward(db, user)
    entry = None
    if intent.sale:
        entry = accounting.record_core(db, "sale_renew" if renewal else "sale_new", intent.sale.amount,
            user=user, package=package, purchase_id=purchase.id if purchase else None,
            admin_id=intent.sale.admin_id, actor_admin_id=intent.sale.actor_admin_id,
            payment_method=intent.sale.payment_method, payment_card_id=intent.sale.payment_card_id)
        db.flush()
    if operation.operation_type == "add_connection" and reservations:
        raise HTTPException(409, "provisioning_intent_invalid")
    for reservation in reservations:
        payment_reservations.capture(db, reservation.id, sale_ledger_entry_id=entry.id if entry else None, package=package)
    db.flush()
    transitions._write_operation(db, operation, "completed", result_user_id=user.id,
        result_purchase_id=purchase.id if purchase else None, sale_ledger_entry_id=entry.id if entry else None,
        username_claim=None, completed_at=dt.datetime.utcnow())
    # Transient only: the controller must consume AFTER its successful commit.
    operation._reconciliation_after_commit = reconcile
    return operation
