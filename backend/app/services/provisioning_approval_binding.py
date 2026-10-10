"""Private P6 approval precondition; no operation creation or live caller.

This locks runtime rows before the approval row per the owner-approved
2026-10-10 exception. It deliberately stops *before* lease revalidation,
manifest comparison or any business mutation. T1 must perform those checks
and commit its operation with the approval transition in one transaction.
"""
from dataclasses import dataclass
import json
import re

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import bot_auth, provisioning_schema, receipt_approval_intent as intent
from . import receipt_approval_runtime as runtime, receipt_void_schema, resource_leases


_SHAPES = {
    "create_user": ("new", "new_user"),
    "purchase": ("new", "existing_user"),
    "renew_user": ("renew", "renew_user"),
    "renew_purchase": ("renew", "renew_purchase"),
}


@dataclass(frozen=True)
class LockedApproval:
    approval_uuid: str
    execution_version: int
    key_instance_uuid: str | None
    wallet_epoch: int
    wallet_phase: str
    registered_under_mode: str
    manifest_hash: str
    replay_operation_id: int | None = None


def lock_prepare(db, approval_uuid, execution_version, principal, operation_type, *,
                 execution_intent, request_hash=None, tenant_scope_key=None):
    """Lock and validate the immutable binding before P6 T1's lease phase.

    The caller must already have begun a fenced short business transaction
    (SQLite BEGIN IMMEDIATE). Any failure requires the caller to roll back.
    No approval state, money, claim, step or target is written here.
    """
    if (db.get_transaction() is None or
            db.info.get("resource_lease_business_transaction") is not db.get_transaction()):
        raise resource_leases.LeaseProtocolError("business_transaction_not_fenced")
    if (not isinstance(approval_uuid, str) or not approval_uuid or
            type(execution_version) is not int or execution_version < 0 or
            operation_type not in _SHAPES or not isinstance(principal, bot_auth.BotPrincipal) or
            not isinstance(execution_intent, intent.ApprovalIntent)):
        raise HTTPException(422, "provisioning_approval_request_invalid")
    if not intent.is_managed_principal(principal):
        raise HTTPException(403, "execution_principal_mismatch")
    receipt_void_schema.assert_ready()
    provisioning_schema.assert_ready()
    wallet = db.execute(select(rv.wallet_runtime_state.c.phase, rv.wallet_runtime_state.c.epoch)
        .where(rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).one_or_none()
    if wallet is None or wallet.phase not in ("normal", "enforced"):
        raise HTTPException(503, "wallet_phase_unavailable")
    approval_runtime = runtime.read_state(db, lock=True, shared_lock=True)
    if approval_runtime is None or runtime.effective_registration_mode(approval_runtime) == runtime.BLOCKED:
        raise HTTPException(503, "receipt_approval_blocked")
    type_mode = db.execute(select(mp.ProvisioningTypeMode.mode).where(
        mp.ProvisioningTypeMode.operation_type == operation_type).with_for_update(read=True)).scalar_one_or_none()
    if type_mode != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == approval_uuid).with_for_update()).mappings().one_or_none()
    if approval is None:
        raise HTTPException(404, "receipt_approval_not_found")
    if approval["version"] != execution_version:
        raise HTTPException(409, "approval_superseded")
    if (approval["kind"], approval["target_shape"]) != _SHAPES[operation_type]:
        raise HTTPException(409, "approval_precondition_failed")
    # The register-time hash binds the selected immutable intent fields,
    # including payment/card, package, discount/referral codes and ordered
    # connection slots. It does not replace the frozen manifest check below.
    # Reconstruct only the derived target from immutable
    # approval snapshots; never re-resolve a live username for this check.
    if (execution_intent.pending_source_instance_id != approval["pending_source_instance_id"] or
            execution_intent.pending_local_id != approval["pending_local_id"]):
        raise HTTPException(409, "approval_intent_changed")
    registered_target = intent.Target(
        shape=approval["target_shape"], owner_admin_id=approval["owner_admin_id_snapshot"],
        tenant_scope_key=approval["tenant_scope_key"], telegram_id=approval["telegram_id_snapshot"],
        telegram_id_source=approval["telegram_id_source"], user_id=approval["target_user_id_snapshot"])
    try:
        execution_hash = intent.intent_hash(execution_intent, registered_target)
    except (AttributeError, TypeError, ValueError):
        raise HTTPException(422, "provisioning_approval_request_invalid") from None
    if execution_hash != approval["immutable_intent_hash"]:
        raise HTTPException(409, "approval_intent_changed")
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
    # A dedicated in-process bot has no key instance. Matching NULL alone
    # would let it execute another tenant's shared-bot approval. Compare the
    # immutable owner snapshot through the same subtree policy as registration.
    owner = approval["owner_admin_id_snapshot"]
    resolved_owner = bot_auth.resolve_claimed_owner(
        db, principal, owner, endpoint="provisioning_approval")
    if resolved_owner != owner:
        raise HTTPException(403, "execution_scope_mismatch")
    # The approval hash anchors the immutable manifest on both fresh T1 and
    # replay. Never return an old operation over a changed manifest.
    expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
        rv.receipt_approval_expected_effects.c.effect_key,
        rv.receipt_approval_expected_effects.c.requirement,
        rv.receipt_approval_expected_effects.c.expected).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid
        ).with_for_update(read=True)).all()
    try:
        rows = [intent.ManifestRow(row.effect_type, row.effect_key, row.requirement,
                                   json.loads(row.expected)) for row in expected]
        matches = intent.manifest_hash(rows) == approval["manifest_hash"]
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise HTTPException(409, "approval_manifest_changed")
    # A committed T1 changes state to mutating. Its exact retry must find
    # the operation before the registered-only precondition, while a changed
    # payload or tenant must never inherit that operation. This remains a
    # read-only private boundary; the future T1 caller owns the rollback.
    previous = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.approval_uuid == approval_uuid,
        mp.ProvisioningOperation.approval_execution_version == execution_version
    ).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if previous is not None:
        if (not isinstance(request_hash, str) or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None or
                not isinstance(tenant_scope_key, str) or not tenant_scope_key or
                previous.operation_type != operation_type or previous.request_hash != request_hash or
                previous.tenant_scope_key != tenant_scope_key):
            raise HTTPException(409, "provisioning_request_changed")
        if (previous.business_key != f"approval:{approval_uuid}:v:{execution_version}" or
                previous.approval_key_bound != (key_uuid is not None) or
                previous.execution_key_instance_uuid != key_uuid):
            raise HTTPException(409, "approval_binding_lost")
        return LockedApproval(approval_uuid, execution_version, key_uuid, wallet.epoch,
                              wallet.phase, approval["registered_under_mode"], approval["manifest_hash"],
                              replay_operation_id=previous.id)
    if approval["state"] != "registered":
        raise HTTPException(409, "approval_precondition_failed")
    # Old shadow paths could commit a financial row before recording its
    # effect or approval transition. A "registered" row is not proof that
    # nothing happened. Do not let P6 start a second execution over either
    # kind of durable mutation evidence.
    has_effect = db.execute(select(rv.receipt_approval_effects.c.id).where(
        rv.receipt_approval_effects.c.approval_uuid == approval_uuid
    ).limit(1).with_for_update(read=True)).first() is not None
    has_ledger = db.execute(select(models.LedgerEntry.id).where(
        models.LedgerEntry.approval_uuid == approval_uuid
    ).limit(1).with_for_update(read=True)).first() is not None
    if has_effect or has_ledger:
        raise HTTPException(409, "approval_has_effects")
    return LockedApproval(approval_uuid, execution_version, key_uuid, wallet.epoch,
                          wallet.phase, approval["registered_under_mode"], approval["manifest_hash"])
