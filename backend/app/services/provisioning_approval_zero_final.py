"""Private DB-only T_final for the narrow, zero-step receipt sale.

The caller owns one fenced Session transaction and its commit/rollback. This
module has no API, scheduler or worker caller. Every other approval shape is
still unavailable; in particular this must not use the non-approval finalizer,
which creates a Purchase even when the registered manifest has none.
"""
import datetime as dt
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import accounting, bot_auth, provisioning_approval_t1 as t1
from . import provisioning_finalization as final, provisioning_schema
from . import provisioning_transitions as transitions, receipt_approval_effects as effects
from . import receipt_approval_intent as intent, receipt_approval_runtime as runtime
from . import receipt_void_schema, resource_leases, user_ops, wallet_policy
from .provisioning_host import HostIdentity, validate_snapshot


def finish(db, approval_uuid, approval_execution_version, operation_id, version,
           leases, principal, execution_intent, *,
           identity, ownership_epoch, installation_uuid):
    """Stage User, sale, two effects and both terminal states in ONE tx.

    No commit/rollback is hidden here. A committed replay returns the existing
    result without reapplying anything. Failure requires caller rollback.
    """
    if (db.in_transaction() or not isinstance(approval_uuid, str) or not approval_uuid or
            type(approval_execution_version) is not int or approval_execution_version < 0 or
            type(operation_id) is not int or operation_id < 1 or
            type(version) is not int or version < 0 or not isinstance(identity, HostIdentity) or
            not isinstance(principal, bot_auth.BotPrincipal) or
            not intent.is_managed_principal(principal) or
            not isinstance(execution_intent, intent.ApprovalIntent)):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    receipt_void_schema.assert_ready()
    provisioning_schema.assert_ready()
    resource_leases.begin_business(db)
    # L1 before the approval row, as the owner-approved lock-order exception
    # requires. MariaDB reads are current locking reads, not RR snapshots.
    wallet = db.execute(select(rv.wallet_runtime_state.c.phase, rv.wallet_runtime_state.c.epoch)
        .where(rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).one_or_none()
    if wallet is None or wallet.phase not in ("normal", "enforced"):
        raise HTTPException(503, "wallet_phase_unavailable")
    approval_runtime = runtime.read_state(db, lock=True, shared_lock=True)
    if approval_runtime is None or runtime.effective_registration_mode(approval_runtime) != "required":
        raise HTTPException(503, "receipt_approval_mode_unavailable")
    mode = db.scalar(select(mp.ProvisioningTypeMode.mode).where(
        mp.ProvisioningTypeMode.operation_type == "create_user").with_for_update(read=True))
    if mode != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    host = db.execute(select(mp.ProvisioningRuntimeState.__table__).where(
        mp.ProvisioningRuntimeState.id == 1).with_for_update(read=True)).mappings().one_or_none()
    validate_snapshot(host, identity, ownership_epoch, installation_uuid)
    if host["gate_mode"] != "enforced":
        raise HTTPException(503, "gate_not_enforced")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one_or_none()
    if approval is None or approval["version"] != approval_execution_version:
        raise HTTPException(409, "approval_superseded")
    if (approval["kind"], approval["target_shape"], approval["tenant_scope_key"],
            approval["owner_admin_id_snapshot"], approval["registered_under_mode"]) != (
            "new", "new_user", "shared", None, "required"):
        raise HTTPException(409, "approval_t1_shape_not_supported")
    if approval["state"] not in ("mutating", "completed"):
        raise HTTPException(409, "approval_precondition_failed")
    key_uuid = approval["execution_key_instance_uuid"]
    if principal.is_internal:
        if principal.key_id is not None or key_uuid is not None:
            raise HTTPException(403, "execution_principal_mismatch")
    else:
        if principal.key_id is None or key_uuid is None:
            raise HTTPException(403, "execution_principal_mismatch")
        key = db.execute(select(models.ApiKey).where(models.ApiKey.id == principal.key_id)
            .with_for_update()).scalar_one_or_none()
        if (key is None or not key.enabled or key.key_instance_uuid != key_uuid or
                key.key_type != bot_auth.KeyType.REMOTE_SHARED_BOT):
            raise HTTPException(403, "execution_key_revoked")
    if bot_auth.resolve_claimed_owner(db, principal, None, endpoint="provisioning_approval") is not None:
        raise HTTPException(403, "execution_scope_mismatch")
    registered_target = intent.Target("new_user", None, "shared", approval["telegram_id_snapshot"],
        approval["telegram_id_source"], approval["target_user_id_snapshot"])
    if (execution_intent.pending_source_instance_id != approval["pending_source_instance_id"] or
            execution_intent.pending_local_id != approval["pending_local_id"] or
            intent.intent_hash(execution_intent, registered_target) != approval["immutable_intent_hash"]):
        raise HTTPException(409, "approval_intent_changed")
    expected_rows = db.execute(select(rv.receipt_approval_expected_effects).where(
        rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
    ).with_for_update(read=True)).mappings().all()
    try:
        manifest = [intent.ManifestRow(row["effect_type"], row["effect_key"], row["requirement"],
            json.loads(row["expected"])) for row in expected_rows]
        exact = intent.manifest_hash(manifest) == approval["manifest_hash"]
    except (TypeError, ValueError):
        exact = False
    if not exact or {(row.effect_type, row.effect_key, row.requirement) for row in manifest} != {
            ("user_created", "user", intent.REQUIRED), ("ledger_sale", "sale", intent.REQUIRED)}:
        raise HTTPException(409, "approval_manifest_changed")
    digest = t1._digest(approval_uuid, approval["version"], execution_intent, "shared")
    if approval["state"] == "completed":
        # A committed retry is read-only even after its former lease expires.
        done = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.id == operation_id).with_for_update(read=True)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if (done is None or done.state != "completed" or done.operation_type != "create_user" or
                done.approval_uuid != approval_uuid or
                done.approval_execution_version != approval["version"] or
                done.version != version or done.request_hash != digest or
                done.business_key != f"approval:{approval_uuid}:v:{approval['version']}" or
                done.tenant_scope_key != "shared" or done.wallet_epoch_at_start != wallet.epoch or
                done.approval_key_bound != (key_uuid is not None) or
                done.execution_key_instance_uuid != key_uuid or
                done.result_user_id is None or done.sale_ledger_entry_id is None):
            raise HTTPException(409, "provisioning_operation_changed")
        actual_keys = db.execute(select(rv.receipt_approval_effects.c.effect_type,
            rv.receipt_approval_effects.c.effect_key).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid)).all()
        if len(actual_keys) != 2 or set(actual_keys) != {
                ("user_created", "user"), ("ledger_sale", "sale")}:
            raise HTTPException(409, "approval_manifest_changed")
        return done
    # L2 is revalidated before locking operation/target rows (L4).
    if leases is None:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    tokens = tuple(leases)
    keys = {token.resource_key for token in tokens}
    if (len(keys) != len(tokens) or f"provisioning_op:{operation_id}" not in keys or
            any(not key.startswith("customer_identity:") and key != f"provisioning_op:{operation_id}"
                for key in keys) or len({token.owner for token in tokens}) != 1 or
            any(not token.owner.startswith(f"op:{operation_id}:") for token in tokens)):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    operation = db.execute(select(mp.ProvisioningOperation).where(mp.ProvisioningOperation.id == operation_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (operation is None or operation.operation_type != "create_user" or
            operation.approval_uuid != approval_uuid or
            operation.approval_execution_version != approval["version"] or
            operation.version != version or operation.request_hash != digest or
            operation.business_key != f"approval:{approval_uuid}:v:{approval['version']}" or
            operation.tenant_scope_key != "shared" or operation.wallet_epoch_at_start != wallet.epoch or
            operation.approval_key_bound != (key_uuid is not None) or
            operation.execution_key_instance_uuid != key_uuid):
        raise HTTPException(409, "provisioning_operation_changed")
    if operation.state != "prepared" or approval["state"] != "mutating" or (
            operation.target_user_id is not None or
            operation.username_claim != approval["target_username_snapshot"]):
        raise HTTPException(409, "provisioning_operation_changed")
    clock, _ = resource_leases._clock(db)
    now = db.scalar(select(clock))
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now)
    if operation.forward_deadline <= now:
        raise HTTPException(409, "provisioning_forward_deadline_exceeded")
    if db.scalar(select(mp.ProvisioningStep.id).where(
            mp.ProvisioningStep.operation_id == operation_id).limit(1).with_for_update(read=True)) is not None or (
            db.scalar(select(mp.PaymentReservation.id).where(
                mp.PaymentReservation.operation_id == operation_id).limit(1).with_for_update(read=True)) is not None) or (
            db.scalar(select(mp.OneTimePackageClaim.id).where(
                mp.OneTimePackageClaim.operation_id == operation_id).limit(1).with_for_update(read=True)) is not None):
        raise HTTPException(409, "approval_t1_shape_not_supported")
    panel = db.execute(select(models.PanelSettings).where(models.PanelSettings.id == 1)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")
    frozen = final._intent(operation)
    user_expected = next(row.expected for row in manifest if row.effect_type == "user_created")
    sale_expected = next(row.expected for row in manifest if row.effect_type == "ledger_sale")
    if (frozen.user.username != user_expected["username"] or
            frozen.user.telegram_id != user_expected["telegram_id"] or
            frozen.user.owner_admin_id is not None or frozen.package is None or
            frozen.package.id != user_expected["package_id"] or
            frozen.quota_bytes != frozen.package.quota_bytes or
            frozen.quota_bytes != user_expected["quota_bytes"] or
            frozen.duration_days != frozen.package.duration_days or
            frozen.duration_days != (user_expected["days"] or 0) or frozen.sale is None or
            frozen.sale.amount != sale_expected["amount"] or
            frozen.sale.payment_card_id is not None or frozen.sale.payment_method is not None or
            frozen.sale.admin_id is not None or frozen.sale.actor_admin_id is not None or
            frozen.purchase_id is not None or frozen.purchase_batch is not None or
            frozen.comment is not None or frozen.reset_usage or frozen.node_fingerprints):
        raise HTTPException(409, "provisioning_intent_invalid")
    identity_id = db.scalar(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == "shared",
        rv.customer_identities.c.identity_key == f"tg:{frozen.user.telegram_id}"))
    if identity_id is not None and f"customer_identity:{identity_id}" not in keys:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    package = db.execute(select(models.Package).where(models.Package.id == frozen.package.id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if package is None or package.one_time_per_user or package.owner_admin_id is not None:
        raise HTTPException(409, "approval_t1_shape_not_supported")
    if panel is not None and panel.loyalty_purchase_threshold == 1:
        raise HTTPException(409, "approval_t1_shape_not_supported")
    if db.scalar(select(models.User.id).where(models.User.username == frozen.user.username)
            .with_for_update()) is not None:
        raise HTTPException(409, "provisioning_target_changed")
    wallet_policy.can_purchase_telegram(db, frozen.user.telegram_id)
    user = user_ops.create_user_record_core(db, **frozen.user.dict(),
        quota_gb=frozen.quota_bytes / 1024 ** 3, expire_days=frozen.duration_days,
        package_id=frozen.package.id, defer_loyalty=True)
    user.total_quota_bytes = frozen.quota_bytes
    user.max_concurrent_sessions = frozen.package.max_concurrent_sessions   # frozen with the approval
    db.flush()
    # A new user with no connection has no Purchase (Receipt Void 5.3).
    sale_package = models.Package(id=frozen.package.id, name=frozen.package.name)
    entry = accounting.record_core(db, "sale_new", frozen.sale.amount, user=user,
        package=sale_package, payment_method=frozen.sale.payment_method)
    entry.approval_uuid = approval_uuid
    db.flush()
    effects.record_effect(db, approval_uuid, "user_created", "user", user,
        effects.UserCreatedEvidence(frozen.package.id, frozen.quota_bytes,
                                    frozen.duration_days or None))
    effects.record_effect(db, approval_uuid, "ledger_sale", "sale", entry)
    actual = db.execute(select(rv.receipt_approval_effects.c.effect_type,
        rv.receipt_approval_effects.c.effect_key,
        rv.receipt_approval_effects.c.actual_projection).where(
        rv.receipt_approval_effects.c.approval_uuid == approval_uuid).with_for_update()).all()
    if (len(actual) != 2 or {(row.effect_type, row.effect_key) for row in actual} != {
            ("user_created", "user"), ("ledger_sale", "sale")} or
            any(json.loads(row.actual_projection) != effects._resolve_bindings(
                db, approval_uuid, next(m.expected for m in manifest
                    if (m.effect_type, m.effect_key) == (row.effect_type, row.effect_key)))
                for row in actual)):
        raise HTTPException(409, "approval_manifest_changed")
    updated = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == approval["version"],
        rv.receipt_approvals.c.state == "mutating").values(
            state="completed", completed_at=func.now())).rowcount
    if updated != 1:
        raise HTTPException(409, "approval_precondition_failed")
    transitions._write_operation(db, operation, "completed", result_user_id=user.id,
        sale_ledger_entry_id=entry.id, username_claim=None, completed_at=dt.datetime.utcnow())
    db.flush()
    return operation
