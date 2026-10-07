"""DB-only one-time claims (Lifecycle 5.5). No live caller or activation.

Caller owns T1 runtime/resource locks, begins a short writer transaction,
rolls back ANY failure (including UNIQUE/deadlock), and retries the whole
transaction. Claims precede payment reservation and remote work.
"""
import hashlib

from fastapi import HTTPException
from sqlalchemy import or_, select

from .. import models, models_provisioning as mp
from . import provisioning_transitions as transitions
from . import wallet_accounts


def claim_key(kind, *, user_id=None, tenant_scope_key=None, telegram_id=None):
    if kind == "user" and type(user_id) is int and user_id > 0 and tenant_scope_key is None and telegram_id is None:
        canonical = f"user|{user_id}"
    elif kind == "telegram" and user_id is None and isinstance(tenant_scope_key, str) and (
            1 <= len(tenant_scope_key) <= 64) and type(telegram_id) is int and 0 < telegram_id < 2**63:
        canonical = f"telegram|{len(tenant_scope_key)}|{tenant_scope_key}|{telegram_id}"
    else:
        raise HTTPException(422, "one_time_claim_identity_invalid")
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def valid(row):
    try:
        return row.claim_key == claim_key(row.claim_kind, user_id=row.claim_user_id,
            tenant_scope_key=row.claim_tenant_scope_key, telegram_id=row.claim_telegram_id)
    except HTTPException:
        return False


def reserve(db, operation_id, package_id, *, telegram_id=None):
    operation = transitions._operation(db, operation_id)
    if operation.operation_type not in ("create_user", "purchase") or operation.state != "prepared":
        raise HTTPException(409, "one_time_claim_operation_invalid")
    package = db.execute(select(models.Package).where(models.Package.id == package_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if package is None:
        raise HTTPException(404, "one_time_claim_package_missing")
    if not package.one_time_per_user:
        return []
    user = None
    if operation.target_user_id is not None:
        user = db.execute(select(models.User).where(models.User.id == operation.target_user_id)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if user is None:
            raise HTTPException(409, "target_user_missing")
        if wallet_accounts.tenant_scope(db, user.owner_admin_id) != operation.tenant_scope_key:
            raise HTTPException(409, "one_time_claim_identity_mismatch")
        if telegram_id is None:
            telegram_id = user.telegram_id
        elif telegram_id != user.telegram_id:
            raise HTTPException(409, "one_time_claim_identity_mismatch")
    elif operation.operation_type != "create_user":
        raise HTTPException(409, "target_user_missing")
    values = []
    if user is not None:
        values.append(dict(claim_kind="user", claim_user_id=user.id,
            claim_tenant_scope_key=None, claim_telegram_id=None))
    if telegram_id is not None:
        values.append(dict(claim_kind="telegram", claim_user_id=None,
            claim_tenant_scope_key=operation.tenant_scope_key, claim_telegram_id=telegram_id))
    if not values:
        raise HTTPException(422, "one_time_claim_identity_invalid")
    for value in values:
        value["claim_key"] = claim_key(value["claim_kind"], user_id=value["claim_user_id"],
            tenant_scope_key=value["claim_tenant_scope_key"], telegram_id=value["claim_telegram_id"])
    owned = db.execute(select(mp.OneTimePackageClaim).where(
        mp.OneTimePackageClaim.operation_id == operation_id, mp.OneTimePackageClaim.release_seq == 0)
        .with_for_update().execution_options(populate_existing=True)).scalars().all()
    expected_keys = {value["claim_key"] for value in values}
    if any(row.package_id != package_id or row.claim_key not in expected_keys for row in owned):
        raise HTTPException(409, "one_time_claim_request_changed")
    # Keep the historical Purchase check until durable activation has
    # explicitly backfilled old purchases. Do not silently backfill here.
    owners = []
    if user is not None:
        owners.append(models.User.id == user.id)
    if telegram_id is not None:
        owners.append(models.User.telegram_id == telegram_id)
    purchased_owners = db.execute(select(models.User).join(models.Purchase, models.Purchase.user_id == models.User.id)
        .where(models.Purchase.package_id == package_id, or_(*owners))).scalars().all()
    if any(wallet_accounts.tenant_scope(db, owner.owner_admin_id) == operation.tenant_scope_key for owner in purchased_owners):
        raise HTTPException(409, "one_time_package_already_purchased")
    rows = []
    for value in sorted(values, key=lambda item: item["claim_key"]):
        row = db.execute(select(mp.OneTimePackageClaim).where(
            mp.OneTimePackageClaim.package_id == package_id,
            mp.OneTimePackageClaim.claim_key == value["claim_key"], mp.OneTimePackageClaim.release_seq == 0)
            .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
        if row is not None:
            if row.operation_id != operation_id:
                raise HTTPException(409, "one_time_package_already_claimed")
            if not valid(row):
                raise HTTPException(409, "one_time_claim_corrupt")
        else:
            row = mp.OneTimePackageClaim(package_id=package_id, operation_id=operation_id, **value)
            db.add(row)
            db.flush()  # UNIQUE is the final concurrent arbiter, not a prior SELECT.
        rows.append(row)
    return rows


def bind_purchase(db, operation_id, purchase_id):
    operation = transitions._operation(db, operation_id)
    if operation.operation_type not in ("create_user", "purchase") or operation.state != "remote_complete":
        raise HTTPException(409, "one_time_claim_operation_invalid")
    purchase = db.execute(select(models.Purchase).where(models.Purchase.id == purchase_id)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    user = db.get(models.User, purchase.user_id) if purchase is not None else None
    matches = user is not None and wallet_accounts.tenant_scope(db, user.owner_admin_id) == operation.tenant_scope_key
    if operation.target_user_id is None:
        matches = matches and operation.operation_type == "create_user" and user.username == operation.username_claim
    else:
        matches = matches and purchase.user_id == operation.target_user_id
    if not matches:
        raise HTTPException(409, "one_time_claim_purchase_mismatch")
    rows = db.execute(select(mp.OneTimePackageClaim).where(mp.OneTimePackageClaim.operation_id == operation_id)
        .order_by(mp.OneTimePackageClaim.id).with_for_update().execution_options(populate_existing=True)).scalars().all()
    if not rows:
        package = db.get(models.Package, purchase.package_id)
        if package is None or package.one_time_per_user:
            raise HTTPException(409, "one_time_claim_missing")
        return []
    expected_kinds = {"user"} if operation.target_user_id is not None else set()
    if user.telegram_id is not None:
        expected_kinds.add("telegram")
    if len(rows) != len(expected_kinds) or {row.claim_kind for row in rows} != expected_kinds:
        raise HTTPException(409, "one_time_claim_missing")
    for row in rows:
        if not valid(row) or row.release_seq != 0 or row.package_id != purchase.package_id or row.purchase_id not in (None, purchase_id):
            raise HTTPException(409, "one_time_claim_purchase_mismatch")
        if row.claim_kind == "user" and row.claim_user_id != user.id or (
                row.claim_kind == "telegram" and row.claim_telegram_id != user.telegram_id):
            raise HTTPException(409, "one_time_claim_purchase_mismatch")
        row.purchase_id = purchase_id
    db.flush()
    return rows
