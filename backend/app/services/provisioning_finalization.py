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
    discount_code: StrictStr | None = None
    discount_amount: StrictInt | None = None
    note: StrictStr | None = None

    @validator("amount")
    def nonnegative(cls, value):
        if value < 0:
            raise ValueError("invalid sale")
        return value


class ApprovalTerms(_Snapshot):
    """What a receipt-approved operation needs at T_final beyond the sale
    (Lifecycle 7.6): the optional benefits applied from the frozen manifest
    and the card payment recorded in the same commit."""
    list_price: StrictInt = 0
    discount_code: StrictStr | None = None
    referral_code: StrictStr | None = None


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
    # A panel usage reset sold as a package keeps the service's own package
    # (the package only prices the sale) - exactly what the legacy reset does.
    keep_package: StrictBool = False
    # reseller_refund.bind_sale's purchase_count_delta for a new service sold
    # with a reseller debit (the caller states it exactly as its legacy path).
    reseller_count_delta: StrictInt = 0
    created_via: StrictStr | None = None          # create_user: models.User.created_via (e.g. "bot")
    node_fingerprints: dict[str, StrictStr] | None = None
    approval: ApprovalTerms | None = None
    # Panel bulk renewal (routers/users.py bulk_update_users): the package
    # REPLACES the service's limits, the purchase is counted and loyalty
    # applies, and a disabled customer's connections stay as they are.
    bulk: StrictBool = False
    # Panel bulk creation: what the WHOLE batch asked for
    # (provisioning_requests.bulk_fingerprint), so every member's hash and
    # persisted intent bind the batch token to that one request.
    batch_fingerprint: StrictStr | None = None

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


def finish(db, operation_id, version, leases, *, require_contracts=False, approval_row=None):
    """Atomically build/capture/complete; replay never repeats a mutation.
    An approval operation needs its approval row, locked by the caller
    before the leases (provisioning_approval_exec.lock_for): the binding is
    checked here and its effects are recorded in this same transaction."""
    from . import provisioning_approval_exec as approval_exec, provisioning_approval_final as approval_final
    receipt_void_schema.assert_ready()
    wallet_service.writer_phase(db)
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).scalar_one()
    from . import loyalty_service
    loyalty_service.pin_mode(db)         # wallet -> approval runtime, the activation's lock order
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
    approved = operation.approval_uuid is not None
    if approved:
        if not approval_row:
            raise HTTPException(409, "provisioning_approval_unlocked")
        approval_exec.check(db, operation, approval_row)
        if approval_row["state"] != "mutating":
            raise approval_exec.ApprovalBindingLost("approval_binding_lost")
    renewal = operation.operation_type in ("renew_user", "renew_purchase")
    if operation.operation_type not in ("create_user", "purchase", "add_connection", "renew_user", "renew_purchase") or (
            operation.version != version or operation.state != ("prepared" if renewal else "remote_complete")):
        raise HTTPException(409, "provisioning_operation_changed")
    intent = _intent(operation)
    # An approved new customer carries the package's limits itself (T1).
    user_limits = approved and operation.operation_type == "create_user" and intent.package is not None and (
        (intent.quota_bytes, intent.duration_days) == (intent.package.quota_bytes, intent.package.duration_days))
    if approved != (intent.approval is not None) or (
            intent.purchase_id is not None and operation.operation_type not in ("add_connection", "renew_purchase")) or (
            operation.operation_type == "create_user" and intent.package and (intent.quota_bytes or intent.duration_days)
            and not user_limits):
        raise HTTPException(409, "provisioning_intent_invalid")
    reservations = db.execute(select(mp.PaymentReservation).where(mp.PaymentReservation.operation_id == operation.id)
        .order_by(mp.PaymentReservation.payer_kind).with_for_update()
        .execution_options(populate_existing=True)).scalars().all()
    if any(row.state != "reserved" for row in reservations) or (
            any(row.payer_kind == "customer_wallet" for row in reservations) and not intent.sale) or (
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
        if user_limits and not steps:
            # No connection, so no Purchase below: the User's own pool is the
            # package's service and holds its cap frozen with the request,
            # like its quota and expiry. Otherwise the cap is the Purchase's.
            user.max_concurrent_sessions = intent.package.max_concurrent_sessions
        if intent.created_via is not None:
            user.created_via = intent.created_via
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
    if operation.operation_type in ("create_user", "purchase") and package and (steps or not user_limits):
        purchase = user_ops.build_purchase_core(db, user, package, intent.comment,
            count_purchase=operation.operation_type != "create_user", grant_loyalty=False)
        purchase.quota_bytes = intent.package.quota_bytes
        provisioning_claims.bind_purchase(db, operation.id, purchase.id)
    elif intent.purchase_id is not None:
        purchase = _locked(db, models.Purchase, intent.purchase_id)
        if purchase is None or purchase.user_id != user.id:
            raise HTTPException(409, "provisioning_target_changed")
    if steps and purchase is None and operation.operation_type in ("create_user", "add_connection"):
        if operation.operation_type == "add_connection":
            # An explicitly pinned Purchase wins above. A purchase_batch names
            # the service whose connections carry it: exactly one such
            # Purchase of THIS customer (locked) is the destination; several
            # are ambiguous. Without either there is only one unambiguous
            # destination; multiple services never receive a connection at random.
            existing = db.execute(select(models.Purchase).where(
                models.Purchase.user_id == user.id).order_by(models.Purchase.id).with_for_update()).scalars().all()
            if intent.purchase_batch is not None:
                batch_owners = set(db.execute(select(models.Connection.purchase_id).where(
                    models.Connection.user_id == user.id, models.Connection.purchase_batch == intent.purchase_batch,
                    models.Connection.purchase_id.isnot(None))).scalars())
                matching = [row for row in existing if row.id in batch_owners]
                if len(matching) > 1:
                    raise HTTPException(409, "purchase_target_required")
                if matching:
                    existing = matching
            if len(existing) > 1:
                raise HTTPException(409, "purchase_target_required")
            purchase = existing[0] if existing else None
        if purchase is None:
            # A connection may no longer be born on User's legacy pool.
            # Copy the exact user-level allowance, without inventing a
            # package price or additional quota.
            purchase = models.Purchase(
                user_id=user.id, package_id=user.package_id,
                quota_bytes=int(user.total_quota_bytes or 0),
                used_bytes=int(user.used_bytes or 0), expire_at=user.expire_at,
                expire_days_after_first_use=user.expire_days_after_first_use,
                max_concurrent_sessions=user.max_concurrent_sessions,
                reserved_quota_bytes=user.reserved_quota_bytes,
                reserved_duration_days=user.reserved_duration_days,
                reserved_package_id=user.reserved_package_id,
                reserved_created_at=user.reserved_created_at,
                status=user.status,
            )
            db.add(purchase)
            db.flush()
            user.reserved_quota_bytes = None
            user.reserved_duration_days = None
            user.reserved_package_id = None
            user.reserved_created_at = None
            provisioning_claims.bind_purchase(db, operation.id, purchase.id)
    if operation.operation_type == "renew_purchase" and purchase is None:
        raise HTTPException(409, "provisioning_intent_invalid")
    renewal_evidence = [] if approved else None
    if renewal and intent.bulk:
        if approved or purchase is None:
            raise HTTPException(409, "provisioning_intent_invalid")
        if package is not None:
            user_ops.replace_purchase_with_package_core(db, user, purchase, _locked(db, models.Package, package.id),
                                                       reset_usage=intent.reset_usage, reconciliation_sink=reconcile)
        else:
            user_ops.renew_purchase_core(db, purchase, intent.quota_bytes / 1024 ** 3, intent.duration_days,
                                         intent.reset_usage, reconciliation_sink=reconcile)
            if user.status == models.UserStatus.disabled:
                reconcile.clear()
        if package is not None or intent.quota_bytes or intent.duration_days:
            user_ops._count_legacy_purchase(db, user)
            user_ops._maybe_grant_loyalty_reward(db, user)
    elif renewal:
        core = user_ops.renew_purchase_core if purchase is not None else user_ops.renew_user_core
        core(db, purchase if purchase is not None else user, add_gb=intent.quota_bytes / 1024 ** 3,
            add_days=intent.duration_days, reset_usage=intent.reset_usage,
            package_id=package.id if package and not intent.keep_package else None, reconciliation_sink=reconcile,
            evidence_sink=renewal_evidence)
    if intent.node_fingerprints is not None:
        if set(intent.node_fingerprints) != {str(step.node_id) for step in steps}:
            raise HTTPException(409, "provisioning_node_config_changed")
        for node_id, fingerprint in sorted(intent.node_fingerprints.items(), key=lambda item: int(item[0])):
            node = _locked(db, models.Node, int(node_id))
            if node is None or provisioning_contracts.config_fingerprint(node) != fingerprint:
                raise HTTPException(409, "provisioning_node_config_changed")
            if require_contracts:
                for backend in sorted({step.backend for step in steps if step.node_id == node.id}):
                    provisioning_contracts.require_ready(db, node, backend)
    elif require_contracts and steps:
        raise HTTPException(409, "provisioning_node_config_changed")
    connections = []
    for step in steps:
        connections.append((step.slot_key, provisioning_records.build_connection_core(
            db, step.id, step.version, user_id=user.id,
            purchase_id=purchase.id if purchase else None, purchase_batch=intent.purchase_batch)))
    # Legacy loyalty is an effect no manifest describes. Under 'required' the
    # runtime is in loyalty_timing_mode 'payment_event' (Receipt Void P9a):
    # loyalty is then counted at payment by loyalty_sale below (and the
    # legacy grant is a no-op in that mode anyway).
    strict_approval = approved and approval_row.get("registered_under_mode") == "required"
    if operation.operation_type in ("create_user", "purchase") and not strict_approval:
        user_ops._maybe_grant_loyalty_reward(db, user)
    entry = None
    if intent.sale:
        entry = accounting.record_core(db, "sale_renew" if renewal else "sale_new", intent.sale.amount,
            user=user, package=package, purchase_id=purchase.id if purchase else None,
            admin_id=intent.sale.admin_id, actor_admin_id=intent.sale.actor_admin_id,
            payment_method=intent.sale.payment_method, payment_card_id=intent.sale.payment_card_id,
            discount_code=intent.sale.discount_code, discount_amount=intent.sale.discount_amount,
            note=intent.sale.note)
        db.flush()
    if operation.operation_type == "add_connection" and reservations:
        raise HTTPException(409, "provisioning_intent_invalid")
    for reservation in reservations:
        payment_reservations.capture(db, reservation.id, sale_ledger_entry_id=entry.id if entry else None, package=package)
    db.flush()
    if operation.operation_type in ("create_user", "purchase") and purchase is not None and package is not None:
        # The new service's sale basis for a later unpaid-cancellation refund,
        # in the same commit as the sale - exactly what the legacy sale paths
        # bind (reseller_refund.bind_sale; no reseller debit -> nothing).
        from . import reseller_refund
        reseller = next((row for row in reservations if row.payer_kind == "reseller_credit"), None)
        if reseller is not None:
            db.refresh(reseller)
            debit = db.get(models.LedgerEntry, reseller.capture_ledger_entry_id)
            db.refresh(purchase)
            reseller_refund.bind_sale(db, purchase, debit, _locked(db, models.Package, package.id),
                                      sale_entry=entry, purchase_count_delta=intent.reseller_count_delta)
            db.flush()
    # Payment-time loyalty (Receipt Void P9a), same transaction as the sale.
    from . import loyalty_sale
    loyalty = loyalty_sale.record_sale(db, operation, user, entry, reservations, approval_row if approved else None)
    optional = None
    if approved:
        credits = []
        for reservation in reservations:
            db.refresh(reservation)
            if reservation.payer_kind == "reseller_credit":
                credits.append(db.get(models.LedgerEntry, reservation.capture_ledger_entry_id))
        optional = approval_final.record(db, operation, intent, approval_row, user=user, purchase=purchase,
                                         connections=connections, entry=entry, credit_entries=credits,
                                         renewal_evidence=renewal_evidence, loyalty=loyalty)
        db.flush()
    transitions._write_operation(db, operation, "completed", result_user_id=user.id,
        result_purchase_id=purchase.id if purchase else None, sale_ledger_entry_id=entry.id if entry else None,
        username_claim=None, completed_at=dt.datetime.utcnow(), optional_effects=optional)
    # Transient only: the controller must consume AFTER its successful commit.
    operation._reconciliation_after_commit = reconcile
    return operation
