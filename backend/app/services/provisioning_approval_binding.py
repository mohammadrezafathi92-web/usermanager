"""Private P6 approval precondition; no operation creation or live caller.

This locks runtime rows before the approval row per the owner-approved
2026-10-10 exception. It deliberately stops *before* lease revalidation,
manifest comparison or any business mutation. T1 must perform those checks
and commit its operation with the approval transition in one transaction.
"""
from dataclasses import dataclass

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


def lock_prepare(db, approval_uuid, execution_version, principal, operation_type):
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
            operation_type not in _SHAPES or not isinstance(principal, bot_auth.BotPrincipal)):
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
    if approval["state"] != "registered" or (approval["kind"], approval["target_shape"]) != _SHAPES[operation_type]:
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
    return LockedApproval(approval_uuid, execution_version, key_uuid, wallet.epoch,
                          wallet.phase, approval["registered_under_mode"], approval["manifest_hash"])
