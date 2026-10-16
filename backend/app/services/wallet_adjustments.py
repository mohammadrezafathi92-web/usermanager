"""Manual wallet adjustments by an admin (Receipt Void P7, 11.1, 12.3).

The one door through which an admin changes a customer's wallet by hand -
the panel's «تغییر اعتبار» and the bot's admin menu. It replaces the old
"type the new balance into the edit form" (UserUpdate.balance, removed):
every adjustment is a signed amount with a reason and an idempotency token,
so a retry never applies it twice and the books say who did it and why.

Its own short writer transaction (begin as the writer, phase read first):
  normal    legacy generation - one atomic UPDATE of User.balance; a debit
            that the balance does not cover is refused (400);
  enforced  wallet lots - a credit is a manual_adjustment credit source
            (settles open debt first), a debit a manual_adjustment debit
            event captured at once over FIFO lots; insufficient -> 400.
Both record a wallet_operations row (wallet_manual_adjustment,
{account}:{token}) when the Receipt Void schema exists: the same token with
the same request is answered without a second change, another request 409.
Never contacts a node.
"""
from __future__ import annotations

import time
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from .. import models, models_receipt_void as rv
from . import receipt_void_schema, wallet_accounts, wallet_service

MAX_REASON = 255


def _token(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        raise HTTPException(422, "idempotency_key_invalid") from None


def _begin(db) -> str:
    db.rollback()
    if db.get_bind().dialect.name == "sqlite":
        db.execute(text("BEGIN IMMEDIATE"))
    return wallet_service.writer_phase(db)


def adjust(db, user_id: int, amount: int, *, actor: models.AdminUser, reason: str, token) -> int:
    """Apply one signed adjustment; returns the balance after it. Commits."""
    if type(amount) is not int or amount == 0:
        raise HTTPException(422, "wallet_adjustment_amount_invalid")
    reason = (reason or "").strip()
    if not reason or len(reason) > MAX_REASON:
        raise HTTPException(422, "wallet_adjustment_reason_required")
    if actor is None or not getattr(actor, "id", None):
        raise HTTPException(403, "wallet_adjustment_actor_required")
    key = _token(token)
    actor_id, actor_name = actor.id, actor.username
    for attempt in range(1, ATTEMPTS + 1):
        phase = _begin(db)
        user = db.get(models.User, user_id)
        if user is None:
            db.rollback()
            raise HTTPException(404, "کاربر پیدا نشد")
        try:
            balance = _apply(db, user, amount, phase=phase, actor_id=actor_id, actor_name=actor_name,
                             reason=reason, key=key)
            db.commit()
            return balance
        except OperationalError as exc:
            db.rollback()
            # A lock conflict (MariaDB 1213/1205, SQLite busy) is retried as a
            # whole: the same token makes a retry of a committed attempt a replay.
            if attempt == ATTEMPTS or not _transient(exc):
                raise HTTPException(503, "database_busy") from exc
            time.sleep(0.1 * attempt)
        except BaseException:
            db.rollback()
            raise
    raise HTTPException(503, "database_busy")


ATTEMPTS = 3


def _transient(exc) -> bool:
    code = next(iter(getattr(getattr(exc, "orig", None), "args", ()) or ()), None)
    return code in (1020, 1205, 1213) or any(word in str(exc).lower() for word in ("locked", "busy", "deadlock"))


def _apply(db, user, amount, *, phase, actor_id, actor_name, reason, key) -> int:
    from . import wallet_lots
    if not receipt_void_schema.is_ready():
        return _legacy(db, user.id, amount)
    account = db.execute(select(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id == user.id, rv.wallet_accounts.c.tombstoned_at.is_(None))).mappings().first()
    if phase == wallet_service.ENFORCED_PHASE:
        if account is None:
            raise HTTPException(503, "wallet_account_not_ready")
        account = dict(account)
        wallet_lots.fence_identity(db, account["customer_identity_id"])
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == receipt_void_schema.SINGLETON_ID)).scalar_one()
    business_key = f"{account['id'] if account else 'user-' + str(user.id)}:{key}"
    tenant = wallet_accounts.tenant_scope(db, user.owner_admin_id)
    operation_id, replayed = wallet_lots.operation(
        db, "wallet_manual_adjustment", business_key,
        payload={"user_id": user.id, "amount": amount, "reason": reason}, tenant_scope_key=tenant,
        epoch=int(epoch), actor_kind="admin", actor_id=actor_id, reason=reason)
    if replayed:
        return int(db.execute(select(models.User.__table__.c.balance).where(
            models.User.__table__.c.id == user.id)).scalar() or 0)
    if phase == wallet_service.ENFORCED_PHASE:
        wallet_lots.require_enforced(db, epoch=int(epoch))
        if amount > 0:
            ref = wallet_lots.credit(db, account, origin_kind="manual_adjustment", source_key=key, amount=amount,
                                     operation_id=operation_id, actor_admin_id=actor_id, actor_username=actor_name,
                                     reason=reason)
        else:
            ref = wallet_lots.hold(db, account, amount=-amount, hold_ref=key, operation_id=operation_id,
                                   kind="manual_adjustment", actor_admin_id=actor_id, reason=reason)
        balance = wallet_lots.available(db, account["id"])
    else:
        balance, ref = _legacy(db, user.id, amount), None
    wallet_lots.complete(db, operation_id, {"amount": amount, "balance_after": balance, "ref": ref})
    return balance


def _legacy(db, user_id: int, amount: int) -> int:
    if amount > 0:
        after: Optional[int] = wallet_service.credit_atomic(db, user_id, amount, phase_checked=True,
                                                            source_kind=wallet_service.MANUAL_ADJUSTMENT)
        if after is None:
            raise HTTPException(404, "کاربر پیدا نشد")
        return after
    if not wallet_service.debit_atomic(db, user_id, -amount, source_kind=wallet_service.MANUAL_ADJUSTMENT):
        raise HTTPException(400, "موجودی کیف پول کافی نیست")
    return int(db.execute(select(models.User.__table__.c.balance).where(
        models.User.__table__.c.id == user_id)).scalar() or 0)
