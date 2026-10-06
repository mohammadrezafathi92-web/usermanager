"""P5 legacy account lifecycle; no backfill, cutover or financial lots.

Creation and tombstoning belong to the caller's transaction. Accounts
are never reused or deleted. Legacy installations whose Receipt Void
schema is unavailable keep working without creating partial wallet data.
Identity rebind/cutover must be completed before enforcing this generation.
"""
import datetime as dt
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import OperationalError

from .. import models, models_receipt_void as rv
from . import hierarchy, receipt_void_schema, wallet_service


def tenant_scope(db, owner_admin_id):
    if owner_admin_id is None:
        return "shared"
    admin = db.get(models.AdminUser, owner_admin_id)
    if admin is None:
        raise HTTPException(409, "wallet_owner_not_found")
    root = hierarchy.parent_admin_scope_id(admin)
    if root is None:
        raise HTTPException(409, "wallet_owner_scope_unavailable")
    return f"admin:{root}"


def _identity(db, *, lineage, telegram_id, owner_admin_id):
    scope = tenant_scope(db, owner_admin_id)
    key = f"tg:{telegram_id}" if telegram_id is not None else f"private:{lineage}"
    values = dict(tenant_scope_key=scope, identity_key=key,
                  telegram_id=telegram_id, owner_admin_id_snapshot=owner_admin_id)
    dialect = db.get_bind().dialect.name
    # Do not SELECT an absent identity FOR UPDATE: competing inserts can
    # deadlock on InnoDB gap locks. The unique key arbitrates insertion.
    if dialect == "sqlite":
        statement = sqlite_insert(rv.customer_identities).values(**values).on_conflict_do_nothing(
            index_elements=["tenant_scope_key", "identity_key"])
    elif dialect in ("mysql", "mariadb"):
        # This immutable table has only a generated PK and the identity
        # unique key. Do not UPDATE a competing identity: MariaDB's
        # duplicate-update path can abort the transaction with 1020.
        # Values come from the already-flushed User; key lengths are bounded
        # above. An exact current read below validates the resulting row.
        statement = mysql_insert(rv.customer_identities).values(**values).prefix_with("IGNORE")
    else:
        raise HTTPException(503, "wallet_dialect_not_supported")
    current_identity = select(rv.customer_identities).where(
        rv.customer_identities.c.tenant_scope_key == scope,
        rv.customer_identities.c.identity_key == key).with_for_update()
    inserted = db.execute(statement)
    identity = db.execute(current_identity).mappings().one_or_none()
    if (identity is None or identity["telegram_id"] != telegram_id
            or (inserted.rowcount == 1 and identity["owner_admin_id_snapshot"] != owner_admin_id)):
        raise HTTPException(503, "wallet_identity_not_ready")
    return identity["id"]


def create_user_with_wallet(db, **attributes):
    """The single User constructor; never commits or contacts a node."""
    try:
        return _create_user_with_wallet(db, **attributes)
    except OperationalError as exc:
        code = getattr(exc.orig, "args", (None,))[0]
        if db.get_bind().dialect.name in ("mysql", "mariadb") and code in (1020, 1205, 1213):
            # MariaDB snapshot conflicts may roll back the ENTIRE transaction.
            # Never retry just the identity statement or discard caller writes
            # ourselves. The caller must roll back and retry the whole unit.
            raise HTTPException(503, "wallet_creation_retry_required") from exc
        raise


def _create_user_with_wallet(db, **attributes):
    ready = receipt_void_schema.is_ready()
    if ready:
        wallet_service.require_legacy_phase(db)
    user = models.User(**attributes)
    db.add(user)
    db.flush()
    if ready:
        if db.get_bind().dialect.name == "sqlite":
            # The first read did not hold SQLite's writer lock. Revalidate
            # after the User INSERT acquired it, before account creation.
            wallet_service.require_legacy_phase(db)
        lineage = str(uuid.uuid4())
        identity = _identity(db, lineage=lineage, telegram_id=user.telegram_id,
                             owner_admin_id=user.owner_admin_id)
        db.execute(rv.wallet_accounts.insert().values(
            lineage_key=lineage, customer_identity_id=identity, user_id=user.id,
            user_id_snapshot=user.id, username_snapshot=user.username,
            owner_admin_id_snapshot=user.owner_admin_id,
            topup_blocked=bool(user.purchases_blocked),
            topup_blocked_reason=user.purchases_blocked_reason))
    return user


def require_legacy_lifecycle(db):
    if receipt_void_schema.is_ready():
        wallet_service.require_legacy_phase(db)


def prepare_user_deletion(db, user):
    """Capture the live account BEFORE node calls, without a writer lock."""
    require_legacy_lifecycle(db)
    if not receipt_void_schema.is_ready():
        return None
    return db.execute(select(rv.wallet_accounts.c.id).where(
        rv.wallet_accounts.c.user_id == user.id)).scalar_one_or_none()


def tombstone_user_account(db, user, *, account_id):
    """Detach a live account before deleting User; keep its complete history.

    Old users without an account are allowed in the normal generation;
    their backfill is a separate P9 operation. No account is synthesized
    during deletion and no balances or debt rows are changed here.
    """
    if not receipt_void_schema.is_ready():
        return
    wallet_service.require_legacy_phase(db)
    account = rv.wallet_accounts
    if account_id is None:
        current = db.execute(select(account.c.id).where(account.c.user_id == user.id)).scalar_one_or_none()
        if current is not None:
            raise HTTPException(409, "wallet_account_changed")
        return
    changed = db.execute(account.update().where(
        account.c.id == account_id, account.c.user_id == user.id).values(
            user_id=None, tombstoned_at=dt.datetime.utcnow(), version=account.c.version + 1))
    if changed.rowcount != 1:
        # A concurrent deletion/recreation must not detach the new account
        # or let the stale ORM User delete the replacement with the same ID.
        raise HTTPException(409, "wallet_account_changed")
