"""P5 normal-generation identity changes; no financial cutover or commits.

User fields, account binding and audit operation belong to one caller
transaction. The full fenced financial lock protocol remains a P8/P9
prerequisite; this writer refuses every non-normal generation.
"""
import datetime as dt
import hashlib
import json
import sqlite3
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from .. import models, models_receipt_void as rv
from . import receipt_void_schema, wallet_accounts, wallet_service

_UNSET = object()


def _begin(db):
    # Unlike wallet_service.begin_writer, this helper MUST preserve the
    # caller's pending edits (hierarchy updates, other profile fields).
    if db.get_bind().dialect.name == "sqlite":
        raw = db.connection().connection.driver_connection
        if not raw.in_transaction:
            db.execute(text("BEGIN IMMEDIATE"))
    wallet_service.require_legacy_phase(db)


def change(db, user, *, telegram_id=_UNSET, owner_admin_id=_UNSET,
           actor_kind="system", actor_id=None, token=None):
    """No commit/rollback: conflicts require retrying the entire request."""
    with db.no_autoflush:
        try:
            return _change(db, user, telegram_id=telegram_id, owner_admin_id=owner_admin_id,
                           actor_kind=actor_kind, actor_id=actor_id, token=token)
        except OperationalError as exc:
            code = next(iter(getattr(exc.orig, "args", ())), None)
            if db.get_bind().dialect.name in ("mysql", "mariadb") and code in (1020, 1205, 1213):
                raise HTTPException(503, "wallet_identity_retry_required") from exc
            if isinstance(exc.orig, sqlite3.OperationalError) and any(
                    word in str(exc.orig).lower() for word in ("locked", "busy")):
                raise HTTPException(503, "wallet_identity_retry_required") from exc
            raise


def _change(db, user, *, telegram_id, owner_admin_id, actor_kind, actor_id, token):
    old_tg, old_owner = user.telegram_id, user.owner_admin_id
    new_tg = old_tg if telegram_id is _UNSET else telegram_id
    new_owner = old_owner if owner_admin_id is _UNSET else owner_admin_id
    ready = receipt_void_schema.is_ready()
    if not ready:
        user.telegram_id, user.owner_admin_id = new_tg, new_owner
        return None  # legacy schema-unready behavior; no partial wallet writes
    _begin(db)
    account = db.execute(select(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id == user.id)).mappings().one_or_none()
    if account is None:
        user.telegram_id, user.owner_admin_id = new_tg, new_owner
        return None  # old users require the separate P9 backfill
    scope = wallet_accounts.tenant_scope(db, new_owner)
    key = f"tg:{new_tg}" if new_tg is not None else f"private:{account['lineage_key']}"
    payload = json.dumps(dict(user_id=user.id, telegram_id=new_tg, owner_admin_id=new_owner,
                              tenant_scope_key=scope), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode()).hexdigest()
    try:
        operation_token = uuid.UUID(str(token)) if token else uuid.uuid4()
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "wallet_identity_invalid_token")
    business_key = f"{account['id']}:{operation_token}"
    previous = db.execute(select(rv.wallet_operations).where(
        rv.wallet_operations.c.operation_type == "wallet_identity_rebind",
        rv.wallet_operations.c.business_key == business_key)).mappings().one_or_none()
    if previous is not None:
        if previous["request_hash"] != digest:
            raise HTTPException(409, "wallet_identity_request_mismatch")
        if previous["state"] != "completed":
            raise HTTPException(503, "wallet_identity_operation_not_ready")
        return previous["id"]
    target = db.execute(select(rv.customer_identities.c.id).where(
        rv.customer_identities.c.tenant_scope_key == scope,
        rv.customer_identities.c.identity_key == key)).scalar_one_or_none()
    if target is None:
        target = wallet_accounts._identity(db, lineage=account["lineage_key"],
                                           telegram_id=new_tg, owner_admin_id=new_owner)
    source = account["customer_identity_id"]
    for identity_id in sorted({source, target}):
        db.execute(select(rv.customer_identities.c.id).where(
            rv.customer_identities.c.id == identity_id).with_for_update()).scalar_one()
    current = db.execute(select(rv.wallet_accounts).where(
        rv.wallet_accounts.c.id == account["id"]).with_for_update()).mappings().one()
    current_user = db.execute(select(models.User.__table__.c.telegram_id,
        models.User.__table__.c.owner_admin_id).where(models.User.id == user.id).with_for_update()).one_or_none()
    if (current["version"] != account["version"] or current["user_id"] != user.id
            or current_user is None or tuple(current_user) != (old_tg, old_owner)):
        raise HTTPException(409, "wallet_identity_changed")
    if source == target and (old_tg, old_owner) == (new_tg, new_owner):
        return None
    debt = rv.wallet_debts
    if db.execute(select(debt.c.id).where(debt.c.customer_identity_id == source,
        debt.c.amount > debt.c.adjusted_amount + debt.c.settled_amount).limit(1).with_for_update()).first():
        raise HTTPException(409, "wallet_identity_has_debt")
    runtime = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1)).scalar_one()
    now = dt.datetime.utcnow()
    result = db.execute(rv.wallet_operations.insert().values(
        operation_type="wallet_identity_rebind", business_key=business_key, request_hash=digest,
        tenant_scope_key=scope, actor_kind=actor_kind, actor_id=actor_id,
        reason="identity change", state="completed", epoch_at_start=runtime,
        result_snapshot=json.dumps(dict(wallet_account_id=account["id"], from_identity_id=source,
                                        to_identity_id=target)), started_at=now, completed_at=now))
    operation_id = result.inserted_primary_key[0]
    changed = db.execute(rv.wallet_accounts.update().where(
        rv.wallet_accounts.c.id == account["id"], rv.wallet_accounts.c.version == account["version"],
        rv.wallet_accounts.c.user_id == user.id).values(customer_identity_id=target,
            version=rv.wallet_accounts.c.version + 1))
    if changed.rowcount != 1:
        raise HTTPException(409, "wallet_identity_changed")
    cause = "owner_transfer" if old_owner != new_owner or old_tg == new_tg else (
        "telegram_link" if old_tg is None else "telegram_change")
    db.execute(rv.wallet_account_identity_rebinds.insert().values(
        wallet_account_id=account["id"], from_identity_id=source, to_identity_id=target,
        cause=cause, actor_admin_id=actor_id if actor_kind == "admin" else None,
        wallet_operation_id=operation_id))
    user.telegram_id, user.owner_admin_id = new_tg, new_owner
    return operation_id


def synchronize_hierarchy(db, *, owner_ids, actor_id):
    """Rebind live accounts after parent/role changes, in the SAME transaction.

    Flush hierarchy edits before re-reading their scopes. No balances,
    historical owner snapshots, sources or debt rows move to another owner.
    """
    if not receipt_void_schema.is_ready():
        return
    _begin(db)
    db.flush()
    db.expire_all()
    users = db.query(models.User).join(rv.wallet_accounts,
        rv.wallet_accounts.c.user_id == models.User.id).filter(
            models.User.owner_admin_id.in_(owner_ids)).order_by(models.User.id).all()
    for user in users:
        change(db, user, actor_kind="admin", actor_id=actor_id)
