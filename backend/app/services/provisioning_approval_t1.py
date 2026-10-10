"""Private atomic T1 for the first, deliberately narrow approval shape.

Only a receipt-paid, shared-scope create-user with no connection, reseller
charge, one-time claim or optional benefit is supported here. The caller
owns the Session and must commit exactly once on success or roll back on
ANY failure. No live router/API/scheduler calls this module. Other shapes
remain fail-closed until their reservation and finalization paths exist.
"""
import dataclasses
import hashlib
import json

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import provisioning_approval_binding as binding, provisioning_approval_live as live
from . import provisioning_finalization as final, resource_leases, wallet_policy
from .receipt_approval_intent import ApprovalIntent
from .provisioning_host import HostIdentity


def _digest(approval_uuid, version, execution_intent, tenant_scope_key):
    canonical = json.dumps({"approval_uuid": approval_uuid, "version": version,
        "tenant_scope_key": tenant_scope_key, "intent": dataclasses.asdict(execution_intent)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def prepare(db, approval_uuid, execution_version, principal, execution_intent, *,
            tenant_scope_key, identity, ownership_epoch, installation_uuid):
    """Stage operation + approval transition in the caller's single tx.

    A replay returns the old operation id without writing. A live mismatch
    raises ``manifest_precondition_failed``; the caller must roll back and
    perform a separate cancel-only transaction. This function neither
    commits nor cancels on its own.
    """
    if (db.in_transaction() or not isinstance(identity, HostIdentity) or
            not isinstance(tenant_scope_key, str) or tenant_scope_key != "shared" or
            not isinstance(execution_intent, ApprovalIntent)):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    digest = _digest(approval_uuid, execution_version, execution_intent, tenant_scope_key)
    resource_leases.begin_business(db)
    locked = binding.lock_prepare(db, approval_uuid, execution_version, principal, "create_user",
        execution_intent=execution_intent, request_hash=digest, tenant_scope_key=tenant_scope_key,
        identity=identity, ownership_epoch=ownership_epoch, installation_uuid=installation_uuid)
    if locked.replay_operation_id is not None:
        return locked.replay_operation_id, (), True
    if locked.wallet_phase != "normal":
        raise HTTPException(503, "provisioning_approval_mode_unavailable")
    if locked.effective_mode != locked.registered_under_mode:
        raise HTTPException(409, "approval_mode_changed")
    if locked.effective_mode != "required":
        raise HTTPException(503, "provisioning_approval_mode_unavailable")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one()
    if approval["tenant_scope_key"] != tenant_scope_key or approval["owner_admin_id_snapshot"] is not None:
        raise HTTPException(409, "approval_scope_changed")
    expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
        rv.receipt_approval_expected_effects.c.effect_key,
        rv.receipt_approval_expected_effects.c.expected).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
        ).with_for_update(read=True)).all()
    if {(row.effect_type, row.effect_key) for row in expected} != {
            ("user_created", "user"), ("ledger_sale", "sale")} or len(expected) != 2:
        raise HTTPException(409, "approval_t1_shape_not_supported")
    customer = db.execute(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == tenant_scope_key,
        rv.customer_identities.c.identity_key == f"tg:{approval['telegram_id_snapshot']}"
    )).scalar_one_or_none()
    operation = mp.ProvisioningOperation(operation_type="create_user",
        business_key=f"approval:{approval_uuid}:v:{execution_version}", request_hash=digest,
        tenant_scope_key=tenant_scope_key, actor_kind="bot", actor_id=principal.key_id,
        approval_uuid=approval_uuid, approval_execution_version=execution_version,
        approval_key_bound=locked.key_instance_uuid is not None,
        execution_key_instance_uuid=locked.key_instance_uuid,
        intent="{}", target_user_id=None, username_claim=approval["target_username_snapshot"],
        state="prepared", forward_deadline=resource_leases._clock(db, 300)[1],
        wallet_epoch_at_start=locked.wallet_epoch)
    db.add(operation)
    db.flush()
    resource_keys = {f"provisioning_op:{operation.id}"}
    if customer is not None:
        resource_keys.add(f"customer_identity:{customer}")
    leases = tuple(resource_leases.acquire(db, key, f"op:{operation.id}:1") for key in sorted(resource_keys))
    result = live.compare(db, locked, execution_intent, leases)
    if result.mismatch is not None:
        raise HTTPException(409, "manifest_precondition_failed")
    user_expected = next(row.expected for row in result.manifest_rows if row.effect_type == "user_created")
    sale_expected = next(row.expected for row in result.manifest_rows if row.effect_type == "ledger_sale")
    package = db.execute(select(models.Package).where(models.Package.id == user_expected["package_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if package is None or package.one_time_per_user or package.owner_admin_id is not None:
        raise HTTPException(409, "approval_t1_shape_not_supported")
    wallet_policy.can_purchase_telegram(db, approval["telegram_id_snapshot"])
    try:
        frozen = final.FinalIntent(user=final.UserSnapshot(
            username=user_expected["username"], telegram_id=user_expected["telegram_id"],
            owner_admin_id=user_expected["owner_admin_id"]),
            package=final.PackageSnapshot(id=package.id, name=package.name,
                quota_bytes=user_expected["quota_bytes"], duration_days=user_expected["days"] or 0,
                max_concurrent_sessions=package.max_concurrent_sessions),
            sale=final.SaleSnapshot(amount=sale_expected["amount"],
                payment_method="card" if sale_expected["payment_card_id"] is not None else None,
                payment_card_id=sale_expected["payment_card_id"]),
            quota_bytes=user_expected["quota_bytes"], duration_days=user_expected["days"] or 0)
    except (TypeError, ValueError, ValidationError):
        raise HTTPException(409, "approval_t1_intent_invalid") from None
    operation.intent = frozen.json()
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "registered").values(
            state="mutating", mutating_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")
    db.flush()
    return operation.id, leases, False
