"""Private single-transaction receipt approval for one existing Purchase.

This is deliberately a narrow DB-only shape: shared bot, card-paid renewal
without card-pool event, discount, referral, reseller debit or optional reward.
The caller owns commit/rollback and post-commit connection reconciliation.
No API or worker calls this module and it does not change readiness.
"""
import datetime as dt
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import accounting, provisioning_approval_binding as binding
from . import provisioning_approval_live as live, provisioning_approval_t1 as t1
from . import provisioning_finalization as final, provisioning_transitions as transitions
from . import receipt_approval_effects as effects, receipt_approval_intent as intent
from . import resource_leases, user_ops, wallet_policy
from .provisioning_host import HostIdentity


def finish(db, approval_uuid, execution_version, principal, execution_intent, *,
           identity, ownership_epoch, installation_uuid):
    """Stage approval, operation, renewal, sale and effects in one commit.

    A completed exact replay is read-only. Any exception requires the caller
    to roll back; this function never commits or contacts a node.
    """
    if (db.in_transaction() or not isinstance(identity, HostIdentity) or
            not isinstance(execution_intent, intent.ApprovalIntent) or
            type(execution_version) is not int or execution_version < 0):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    if (execution_intent.kind != "renew" or execution_intent.renew_purchase_id is None or
            execution_intent.payment_card_id is not None or
            (execution_intent.discount_code or "").strip() or
            (execution_intent.referral_code or "").strip() or execution_intent.connections):
        raise HTTPException(409, "approval_renew_shape_not_supported")
    digest = t1._digest(approval_uuid, execution_version, execution_intent, "shared")
    resource_leases.begin_business(db)
    locked = binding.lock_prepare(db, approval_uuid, execution_version, principal, "renew_purchase",
        execution_intent=execution_intent, request_hash=digest, tenant_scope_key="shared",
        identity=identity, ownership_epoch=ownership_epoch, installation_uuid=installation_uuid)
    if locked.replay_operation_id is not None:
        previous = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.id == locked.replay_operation_id).with_for_update(read=True)
            .execution_options(populate_existing=True)).scalar_one()
        if previous.state != "completed" or previous.result_purchase_id != execution_intent.renew_purchase_id:
            raise HTTPException(409, "provisioning_operation_changed")
        return previous, True
    if locked.wallet_phase != "normal" or locked.effective_mode != "required" or (
            locked.registered_under_mode != "required"):
        raise HTTPException(503, "provisioning_approval_mode_unavailable")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one()
    if (approval["tenant_scope_key"] != "shared" or approval["owner_admin_id_snapshot"] is not None or
            approval["target_user_id_snapshot"] is None):
        raise HTTPException(409, "approval_scope_changed")
    expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
        rv.receipt_approval_expected_effects.c.effect_key,
        rv.receipt_approval_expected_effects.c.expected).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
        ).with_for_update(read=True)).all()
    if len(expected) != 2 or {(row.effect_type, row.effect_key) for row in expected} != {
            ("purchase_renewed", "renew"), ("ledger_sale", "sale")}:
        raise HTTPException(409, "approval_renew_shape_not_supported")
    customer_id = db.execute(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == "shared",
        rv.customer_identities.c.identity_key == f"tg:{approval['telegram_id_snapshot']}"
    )).scalar_one_or_none()
    if customer_id is None:
        raise HTTPException(409, "provisioning_target_changed")
    operation = mp.ProvisioningOperation(operation_type="renew_purchase",
        business_key=f"approval:{approval_uuid}:v:{execution_version}", request_hash=digest,
        tenant_scope_key="shared", actor_kind="bot", actor_id=principal.key_id,
        approval_uuid=approval_uuid, approval_execution_version=execution_version,
        approval_key_bound=locked.key_instance_uuid is not None,
        execution_key_instance_uuid=locked.key_instance_uuid, intent="{}",
        target_user_id=approval["target_user_id_snapshot"], username_claim=None,
        state="prepared", forward_deadline=resource_leases._clock(db, 300)[1],
        wallet_epoch_at_start=locked.wallet_epoch)
    db.add(operation)
    db.flush()
    owner = f"op:{operation.id}:1"
    leases = tuple(resource_leases.acquire(db, key, owner) for key in sorted((
        f"customer_identity:{customer_id}", f"provisioning_op:{operation.id}")))
    compared = live.compare(db, locked, execution_intent, leases)
    if compared.mismatch is not None:
        raise HTTPException(409, "manifest_precondition_failed")
    renew_expected = next(row.expected for row in compared.manifest_rows if row.effect_type == "purchase_renewed")
    sale_expected = next(row.expected for row in compared.manifest_rows if row.effect_type == "ledger_sale")
    if (renew_expected["purchase_id"] != execution_intent.renew_purchase_id or
            renew_expected["user_id"] != approval["target_user_id_snapshot"] or
            renew_expected["add_quota_bytes"] < 0 or renew_expected["add_days"] < 0 or
            (renew_expected["add_quota_bytes"] == 0 and renew_expected["add_days"] == 0) or
            sale_expected["payment_card_id"] is not None):
        raise HTTPException(409, "approval_renew_shape_not_supported")
    user = db.execute(select(models.User).where(models.User.id == renew_expected["user_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    purchase = db.execute(select(models.Purchase).where(models.Purchase.id == renew_expected["purchase_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    package = db.execute(select(models.Package).where(models.Package.id == renew_expected["package_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (user is None or purchase is None or purchase.user_id != user.id or package is None or
            package.owner_admin_id is not None or package.one_time_per_user):
        raise HTTPException(409, "provisioning_target_changed")
    wallet_policy.can_purchase(db, user)
    frozen = final.FinalIntent(user=final.UserSnapshot(username=user.username,
        telegram_id=user.telegram_id, owner_admin_id=None),
        package=final.PackageSnapshot(id=package.id, name=package.name,
            quota_bytes=renew_expected["add_quota_bytes"], duration_days=renew_expected["add_days"],
            max_concurrent_sessions=package.max_concurrent_sessions),
        sale=final.SaleSnapshot(amount=sale_expected["amount"]),
        purchase_id=purchase.id, quota_bytes=renew_expected["add_quota_bytes"],
        duration_days=renew_expected["add_days"])
    operation.intent = frozen.json()
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "registered").values(
            state="mutating", mutating_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")
    evidence = []
    user_ops.renew_purchase_core(db, purchase, add_gb=renew_expected["add_quota_bytes"] / 1024 ** 3,
        add_days=renew_expected["add_days"], package_id=package.id, evidence_sink=evidence,
        reconciliation_sink=[])
    if len(evidence) != 1:
        raise HTTPException(409, "approval_renew_evidence_invalid")
    entry = accounting.record_core(db, "sale_renew", sale_expected["amount"], user=user,
        package=package, purchase_id=purchase.id)
    entry.approval_uuid = approval_uuid
    db.flush()
    effects.record_effect(db, approval_uuid, "purchase_renewed", "renew", purchase, evidence[0])
    effects.record_effect(db, approval_uuid, "ledger_sale", "sale", entry)
    actual = db.execute(select(rv.receipt_approval_effects.c.effect_type,
        rv.receipt_approval_effects.c.effect_key,
        rv.receipt_approval_effects.c.actual_projection).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid).with_for_update()).all()
    if (len(actual) != 2 or {(row.effect_type, row.effect_key) for row in actual} != {
            ("purchase_renewed", "renew"), ("ledger_sale", "sale")} or
            any(json.loads(row.actual_projection) != effects._resolve_bindings(
                db, approval_uuid, next(item.expected for item in compared.manifest_rows
                    if (item.effect_type, item.effect_key) == (row.effect_type, row.effect_key)))
                for row in actual)):
        raise HTTPException(409, "approval_manifest_changed")
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "mutating").values(
            state="completed", completed_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")
    transitions._write_operation(db, operation, "completed", result_user_id=user.id,
        result_purchase_id=purchase.id, sale_ledger_entry_id=entry.id,
        completed_at=dt.datetime.utcnow())
    db.flush()
    return operation, False
