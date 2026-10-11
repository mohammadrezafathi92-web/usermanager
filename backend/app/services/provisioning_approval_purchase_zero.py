"""Private receipt-approved existing-user purchase with no remote steps.

T1 and T_final are separate caller-owned transactions. This deliberately
supports only the shared, no-connection, no-optional-benefit shape; there is
no API, scheduler, worker, or mode activation here.
"""
import datetime as dt
import json

from fastapi import HTTPException
from sqlalchemy import func, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import accounting, bot_auth, provisioning_approval_binding as binding
from . import provisioning_approval_live as live, provisioning_approval_t1 as t1
from . import provisioning_finalization as final, provisioning_schema
from . import provisioning_transitions as transitions, receipt_approval_effects as effects
from . import receipt_approval_intent as intent, receipt_approval_runtime as runtime
from . import receipt_void_schema, resource_leases, user_ops, wallet_policy
from .provisioning_host import HostIdentity, validate_snapshot


def _shape(execution_intent):
    if (not isinstance(execution_intent, intent.ApprovalIntent) or
            execution_intent.kind != "new" or execution_intent.connections or
            execution_intent.payment_card_id is not None or
            (execution_intent.discount_code or "").strip() or
            (execution_intent.referral_code or "").strip()):
        raise HTTPException(409, "approval_purchase_shape_not_supported")


def prepare(db, approval_uuid, execution_version, principal, execution_intent, *,
            identity, ownership_epoch, installation_uuid):
    """Stage approval mutating + operation prepared; never commit here."""
    if (db.in_transaction() or not isinstance(identity, HostIdentity) or
            type(execution_version) is not int or execution_version < 0):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    _shape(execution_intent)
    digest = t1._digest(approval_uuid, execution_version, execution_intent, "shared")
    resource_leases.begin_business(db)
    locked = binding.lock_prepare(db, approval_uuid, execution_version, principal, "purchase",
        execution_intent=execution_intent, request_hash=digest, tenant_scope_key="shared",
        identity=identity, ownership_epoch=ownership_epoch, installation_uuid=installation_uuid)
    if locked.replay_operation_id is not None:
        previous = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.id == locked.replay_operation_id).with_for_update(read=True)
            .execution_options(populate_existing=True)).scalar_one()
        if previous.state not in ("prepared", "completed"):
            raise HTTPException(409, "provisioning_operation_changed")
        return previous.id, (), True
    if (locked.wallet_phase != "normal" or locked.effective_mode != "required" or
            locked.registered_under_mode != "required"):
        raise HTTPException(503, "provisioning_approval_mode_unavailable")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one()
    if (approval["tenant_scope_key"] != "shared" or approval["owner_admin_id_snapshot"] is not None or
            approval["target_user_id_snapshot"] is None):
        raise HTTPException(409, "approval_scope_changed")
    expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
        rv.receipt_approval_expected_effects.c.effect_key).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
        ).with_for_update(read=True)).all()
    if len(expected) != 2 or {(row.effect_type, row.effect_key) for row in expected} != {
            ("purchase_created", "purchase"), ("ledger_sale", "sale")}:
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    customer_id = db.scalar(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == "shared",
        rv.customer_identities.c.identity_key == f"tg:{approval['telegram_id_snapshot']}"))
    if customer_id is None:
        raise HTTPException(409, "provisioning_target_changed")
    operation = mp.ProvisioningOperation(operation_type="purchase",
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
    purchase_expected = next(row.expected for row in compared.manifest_rows if row.effect_type == "purchase_created")
    sale_expected = next(row.expected for row in compared.manifest_rows if row.effect_type == "ledger_sale")
    user = db.execute(select(models.User).where(models.User.id == approval["target_user_id_snapshot"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    package = db.execute(select(models.Package).where(models.Package.id == purchase_expected["package_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (user is None or user.owner_admin_id is not None or package is None or
            package.owner_admin_id is not None or package.one_time_per_user or
            purchase_expected["user"] != {"type": "User", "id": user.id} or
            sale_expected["payment_card_id"] is not None):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    # build_purchase_core absorbs only unlinked connections. Historical
    # user-level counters can remain after migration; they do not create an
    # extra Purchase and must not block a migrated customer's new purchase.
    if any(c.purchase_id is None for c in user.connections):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    panel = db.get(models.PanelSettings, 1)
    threshold = panel.loyalty_purchase_threshold if panel is not None else None
    if threshold and (user.purchase_count or 0) + 1 >= threshold * ((user.loyalty_rewards_given or 0) + 1):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    wallet_policy.can_purchase(db, user)
    frozen = final.FinalIntent(user=final.UserSnapshot(username=user.username,
        telegram_id=user.telegram_id, owner_admin_id=None),
        package=final.PackageSnapshot(id=package.id, name=package.name,
            quota_bytes=purchase_expected["quota_bytes"],
            duration_days=purchase_expected["days"] or 0,
            max_concurrent_sessions=package.max_concurrent_sessions),
        sale=final.SaleSnapshot(amount=sale_expected["amount"]),
        quota_bytes=purchase_expected["quota_bytes"],
        duration_days=purchase_expected["days"] or 0)
    operation.intent = frozen.json()
    changed = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "registered").values(
            state="mutating", mutating_at=func.now())).rowcount
    if changed != 1:
        raise HTTPException(409, "approval_precondition_failed")
    db.flush()
    return operation.id, leases, False


def finish(db, approval_uuid, execution_version, operation_id, version, leases,
           principal, execution_intent, *, identity, ownership_epoch, installation_uuid):
    """Stage the new Purchase, sale and exact effects in one T_final commit."""
    if (db.in_transaction() or not isinstance(identity, HostIdentity) or
            not isinstance(principal, bot_auth.BotPrincipal) or
            not intent.is_managed_principal(principal) or
            type(execution_version) is not int or execution_version < 0 or
            type(operation_id) is not int or operation_id < 1 or
            type(version) is not int or version < 0 or
            not isinstance(execution_intent, intent.ApprovalIntent)):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    _shape(execution_intent)
    receipt_void_schema.assert_ready()
    provisioning_schema.assert_ready()
    resource_leases.begin_business(db)
    wallet = db.execute(select(rv.wallet_runtime_state.c.phase, rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).one_or_none()
    if wallet is None or wallet.phase != "normal":
        raise HTTPException(503, "wallet_phase_unavailable")
    receipt = runtime.read_state(db, lock=True, shared_lock=True)
    if receipt is None or runtime.effective_registration_mode(receipt) != "required":
        raise HTTPException(503, "receipt_approval_mode_unavailable")
    if db.scalar(select(mp.ProvisioningTypeMode.mode).where(
            mp.ProvisioningTypeMode.operation_type == "purchase").with_for_update(read=True)) != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    host = db.execute(select(mp.ProvisioningRuntimeState.__table__).where(
        mp.ProvisioningRuntimeState.id == 1).with_for_update(read=True)).mappings().one_or_none()
    validate_snapshot(host, identity, ownership_epoch, installation_uuid)
    if host["gate_mode"] != "enforced":
        raise HTTPException(503, "gate_not_enforced")
    panel = db.execute(select(models.PanelSettings).where(models.PanelSettings.id == 1)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one_or_none()
    if approval is None or approval["version"] != execution_version:
        raise HTTPException(409, "approval_superseded")
    if (approval["kind"], approval["target_shape"], approval["tenant_scope_key"],
            approval["owner_admin_id_snapshot"], approval["registered_under_mode"]) != (
            "new", "existing_user", "shared", None, "required"):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    digest = t1._digest(approval_uuid, execution_version, execution_intent, "shared")
    # The binding was checked in T1. A changed/revoked key must not complete
    # an already-mutating approval under a different principal.
    bound = approval["execution_key_instance_uuid"]
    if principal.is_internal:
        if principal.key_id is not None or bound is not None:
            raise HTTPException(403, "execution_principal_mismatch")
    else:
        key = db.execute(select(models.ApiKey).where(models.ApiKey.id == principal.key_id)
            .with_for_update()).scalar_one_or_none()
        if key is None or not key.enabled or key.key_instance_uuid != bound or (
                key.key_type != bot_auth.KeyType.REMOTE_SHARED_BOT):
            raise HTTPException(403, "execution_key_revoked")
    if bot_auth.resolve_claimed_owner(db, principal, None, endpoint="provisioning_approval") is not None:
        raise HTTPException(403, "execution_scope_mismatch")
    target = intent.Target("existing_user", None, "shared", approval["telegram_id_snapshot"],
        approval["telegram_id_source"], approval["target_user_id_snapshot"])
    if (execution_intent.pending_source_instance_id != approval["pending_source_instance_id"] or
            execution_intent.pending_local_id != approval["pending_local_id"] or
            intent.intent_hash(execution_intent, target) != approval["immutable_intent_hash"]):
        raise HTTPException(409, "approval_intent_changed")
    expected = db.execute(select(rv.receipt_approval_expected_effects).where(
        rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
    ).with_for_update(read=True)).mappings().all()
    manifest = tuple(intent.ManifestRow(row["effect_type"], row["effect_key"],
        row["requirement"], json.loads(row["expected"])) for row in expected)
    if (intent.manifest_hash(list(manifest)) != approval["manifest_hash"] or len(manifest) != 2 or
            {(row.effect_type, row.effect_key, row.requirement) for row in manifest} != {
                ("purchase_created", "purchase", intent.REQUIRED),
                ("ledger_sale", "sale", intent.REQUIRED)}):
        raise HTTPException(409, "approval_manifest_changed")
    if approval["state"] == "completed":
        done = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.id == operation_id).with_for_update(read=True)
            .execution_options(populate_existing=True)).scalar_one_or_none()
        if (done is None or done.state != "completed" or done.operation_type != "purchase" or
                done.approval_uuid != approval_uuid or
                done.approval_execution_version != execution_version or
                done.version != version or done.request_hash != digest or
                done.business_key != f"approval:{approval_uuid}:v:{execution_version}" or
                done.tenant_scope_key != "shared" or done.wallet_epoch_at_start != wallet.epoch or
                done.approval_key_bound != (bound is not None) or
                done.execution_key_instance_uuid != bound or
                done.target_user_id != approval["target_user_id_snapshot"] or
                done.result_purchase_id is None or done.sale_ledger_entry_id is None):
            raise HTTPException(409, "provisioning_operation_changed")
        actual_keys = db.execute(select(rv.receipt_approval_effects.c.effect_type,
            rv.receipt_approval_effects.c.effect_key).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid)).all()
        if len(actual_keys) != 2 or set(actual_keys) != {
                ("purchase_created", "purchase"), ("ledger_sale", "sale")}:
            raise HTTPException(409, "approval_manifest_changed")
        return done
    if leases is None:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    tokens = tuple(leases)
    keys = {token.resource_key for token in tokens}
    if (len(tokens) != 2 or f"provisioning_op:{operation_id}" not in keys or
            len([key for key in keys if key.startswith("customer_identity:")]) != 1 or
            len({token.owner for token in tokens}) != 1 or
            any(not token.owner.startswith(f"op:{operation_id}:") for token in tokens)):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    customer_id = db.scalar(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == "shared",
        rv.customer_identities.c.identity_key == f"tg:{approval['telegram_id_snapshot']}"))
    if customer_id is None or f"customer_identity:{customer_id}" not in keys:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    operation = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.id == operation_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if (operation is None or operation.operation_type != "purchase" or
            operation.approval_uuid != approval_uuid or operation.approval_execution_version != execution_version or
            operation.version != version or operation.request_hash != digest or
            operation.business_key != f"approval:{approval_uuid}:v:{execution_version}" or
            operation.tenant_scope_key != "shared" or operation.wallet_epoch_at_start != wallet.epoch or
            operation.approval_key_bound != (bound is not None) or
            operation.execution_key_instance_uuid != bound or
            operation.target_user_id != approval["target_user_id_snapshot"] or
            operation.state != "prepared" or approval["state"] != "mutating"):
        raise HTTPException(409, "provisioning_operation_changed")
    clock, _ = resource_leases._clock(db)
    now = db.scalar(select(clock))
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now)
    if operation.forward_deadline <= now:
        raise HTTPException(409, "provisioning_forward_deadline_exceeded")
    for table in (mp.ProvisioningStep, mp.PaymentReservation, mp.OneTimePackageClaim):
        if db.scalar(select(table.id).where(table.operation_id == operation_id)
                .limit(1).with_for_update(read=True)) is not None:
            raise HTTPException(409, "approval_purchase_shape_not_supported")
    frozen = final._intent(operation)
    purchase_expected = next(row.expected for row in manifest if row.effect_type == "purchase_created")
    sale_expected = next(row.expected for row in manifest if row.effect_type == "ledger_sale")
    user = db.execute(select(models.User).where(models.User.id == operation.target_user_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    package = db.execute(select(models.Package).where(models.Package.id == purchase_expected["package_id"])
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if (user is None or user.owner_admin_id is not None or package is None or
            package.owner_admin_id is not None or package.one_time_per_user or
            frozen.user.username != user.username or frozen.user.telegram_id != user.telegram_id or
            frozen.user.owner_admin_id is not None or
            frozen.package is None or frozen.package.id != package.id or
            frozen.package.name != package.name or
            frozen.package.max_concurrent_sessions != package.max_concurrent_sessions or
            frozen.package.quota_bytes != user_ops.gb_to_bytes(package.quota_gb or 0) or
            frozen.package.duration_days != (package.duration_days or 0) or
            frozen.package.quota_bytes != purchase_expected["quota_bytes"] or
            frozen.package.duration_days != (purchase_expected["days"] or 0) or
            frozen.quota_bytes != frozen.package.quota_bytes or
            frozen.duration_days != frozen.package.duration_days or
            frozen.sale is None or frozen.sale.amount != sale_expected["amount"] or
            frozen.sale.payment_card_id is not None or frozen.sale.payment_method is not None or
            frozen.sale.admin_id is not None or frozen.sale.actor_admin_id is not None or
            frozen.purchase_id is not None or frozen.purchase_batch is not None or
            frozen.comment is not None or frozen.reset_usage or frozen.node_fingerprints):
        raise HTTPException(409, "provisioning_target_changed")
    # Recheck the exact same absorption hazard under T_final's target lock.
    if any(c.purchase_id is None for c in user.connections):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    threshold = panel.loyalty_purchase_threshold if panel is not None else None
    if threshold and (user.purchase_count or 0) + 1 >= threshold * ((user.loyalty_rewards_given or 0) + 1):
        raise HTTPException(409, "approval_purchase_shape_not_supported")
    wallet_policy.can_purchase(db, user)
    purchase = user_ops.build_purchase_core(db, user, package,
        count_purchase=True, grant_loyalty=False)
    purchase.quota_bytes = frozen.package.quota_bytes
    db.flush()
    entry = accounting.record_core(db, "sale_new", frozen.sale.amount,
        user=user, package=package, purchase_id=purchase.id)
    entry.approval_uuid = approval_uuid
    db.flush()
    effects.record_effect(db, approval_uuid, "purchase_created", "purchase", purchase,
        effects.PurchaseCreatedEvidence(frozen.package.duration_days or None))
    effects.record_effect(db, approval_uuid, "ledger_sale", "sale", entry)
    actual = db.execute(select(rv.receipt_approval_effects.c.effect_type,
        rv.receipt_approval_effects.c.effect_key,
        rv.receipt_approval_effects.c.actual_projection).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid).with_for_update()).all()
    if (len(actual) != 2 or {(row.effect_type, row.effect_key) for row in actual} != {
            ("purchase_created", "purchase"), ("ledger_sale", "sale")} or
            any(json.loads(row.actual_projection) != effects._resolve_bindings(
                db, approval_uuid, next(item.expected for item in manifest
                    if (item.effect_type, item.effect_key) == (row.effect_type, row.effect_key)))
                for row in actual)):
        raise HTTPException(409, "approval_manifest_changed")
    changed = db.execute(rv.receipt_approvals.update().where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid,
        rv.receipt_approvals.c.version == execution_version,
        rv.receipt_approvals.c.state == "mutating").values(
            state="completed", completed_at=func.now())).rowcount
    if changed != 1:
        raise HTTPException(409, "approval_precondition_failed")
    transitions._write_operation(db, operation, "completed", result_user_id=user.id,
        result_purchase_id=purchase.id, sale_ledger_entry_id=entry.id,
        completed_at=dt.datetime.utcnow())
    db.flush()
    return operation
