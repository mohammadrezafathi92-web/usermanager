"""Private, read-only live preflight for an approval T1; no live caller.

Run only after ``lock_prepare`` and acquisition of the full resource-lease
set, in the same fenced transaction. A mismatch is data for T1's separate
cancel-only commit; this module never changes approval state or reserves money.
"""
from dataclasses import dataclass
import json

from sqlalchemy import select

from .. import models, models_receipt_void as rv
from . import receipt_approval_intent as intent, resource_leases
from .provisioning_approval_binding import LockedApproval


@dataclass(frozen=True)
class LivePreflight:
    mismatch: str | None
    manifest_rows: tuple[intent.ManifestRow, ...] = ()


def compare(db, locked: LockedApproval, execution_intent: intent.ApprovalIntent, leases) -> LivePreflight:
    """Compare locked target and freshly projected manifest to registration.

    The caller must not run this on a replay: a committed operation owns its
    frozen intent already. This check is deliberately not an authorization or
    a T1 implementation. Any database error must roll back the whole caller.
    """
    if not isinstance(locked, LockedApproval) or not isinstance(execution_intent, intent.ApprovalIntent):
        raise resource_leases.LeaseProtocolError("approval_preflight_invalid")
    if locked.replay_operation_id is not None:
        raise resource_leases.LeaseProtocolError("approval_preflight_replay")
    resource_leases.revalidate(db, leases)
    approval = db.execute(select(rv.receipt_approvals).where(
        rv.receipt_approvals.c.approval_uuid == locked.approval_uuid).with_for_update()).mappings().one_or_none()
    if approval is None or approval["version"] != locked.execution_version or approval["state"] != "registered":
        return LivePreflight("approval_precondition_failed")
    target = intent.Target(
        shape=approval["target_shape"], owner_admin_id=approval["owner_admin_id_snapshot"],
        tenant_scope_key=approval["tenant_scope_key"], telegram_id=approval["telegram_id_snapshot"],
        telegram_id_source=approval["telegram_id_source"], user_id=approval["target_user_id_snapshot"])
    if intent.tenant_scope_key(db, target.owner_admin_id) != target.tenant_scope_key:
        return LivePreflight("target_tenant_changed")
    if target.shape == "new_user":
        occupied = db.execute(select(models.User.id).where(
            models.User.username == approval["target_username_snapshot"]).with_for_update()).first()
        if occupied:
            return LivePreflight("username_taken")
    else:
        user = db.execute(select(models.User).where(models.User.id == target.user_id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if user is None or (user.username, user.owner_admin_id, user.telegram_id) != (
                approval["target_username_snapshot"], target.owner_admin_id, target.telegram_id):
            return LivePreflight("target_user_changed")
        if target.shape == "renew_purchase":
            purchase = db.execute(select(models.Purchase).where(
                models.Purchase.id == execution_intent.renew_purchase_id).with_for_update()
                .execution_options(populate_existing=True)).scalar_one_or_none()
            if purchase is None or purchase.user_id != user.id or purchase.package_id != execution_intent.package_id:
                return LivePreflight("target_purchase_changed")
        if target.shape == "renew_user":
            # Registration did not persist its Purchase count. Comparing a
            # current count with a guessed baseline would silently accept a
            # changed renewal target. Fail closed until that snapshot exists.
            return LivePreflight("target_shape_unverifiable")
        if target.shape == "topup" and locked.wallet_phase == "enforced":
            account = db.execute(select(rv.wallet_accounts.c.id).where(
                rv.wallet_accounts.c.user_id == user.id,
                rv.wallet_accounts.c.tombstoned_at.is_(None)).with_for_update()).first()
            if account is None:
                return LivePreflight("target_user_changed")
    try:
        rows = tuple(intent.build_manifest(db, execution_intent, target))
    except intent.IntentRejected:
        return LivePreflight("manifest_precondition_failed")
    expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
        rv.receipt_approval_expected_effects.c.effect_key,
        rv.receipt_approval_expected_effects.c.requirement,
        rv.receipt_approval_expected_effects.c.expected).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == locked.approval_uuid
        ).with_for_update(read=True)).all()
    try:
        registered = tuple(intent.ManifestRow(row.effect_type, row.effect_key, row.requirement,
            json.loads(row.expected)) for row in expected)
        exact = intent.manifest_hash(list(rows)) == intent.manifest_hash(list(registered)) == locked.manifest_hash
    except (TypeError, ValueError):
        exact = False
    if not exact:
        return LivePreflight("manifest_precondition_failed")
    for row in rows:
        if row.effect_type != "connection_created":
            continue
        node = db.execute(select(models.Node).where(models.Node.id == row.expected["node_id"])
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if node is None or not node.enabled:
            return LivePreflight("manifest_precondition_failed")
    return LivePreflight(None, rows)
