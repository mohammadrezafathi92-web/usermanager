"""One policy boundary for customer purchases and wallet top-ups (P5).

Legacy purchase locks keep their existing top-up meaning until cutover.
In the enforced generation a fraud purchase lock does not prevent debt
repayment: only the wallet account's explicit top-up lock does that.
These checks never commit, write, or contact a node.
"""
from fastapi import HTTPException
from sqlalchemy import select, func

from .. import models, models_receipt_void as rv
from . import receipt_void_schema

DEFAULT_PURCHASE_BLOCK_MESSAGE = (
    "امکان خرید و تمدید برای این حساب فعلا غیرفعال است. "
    "سرویس فعلی شما تا پایان اعتبارش کار می‌کند. برای اطلاعات بیشتر با پشتیبانی تماس بگیرید."
)


def _purchase_lock(user):
    if getattr(user, "purchases_blocked", False):
        raise HTTPException(403, (user.purchases_blocked_reason or "").strip()
                            or DEFAULT_PURCHASE_BLOCK_MESSAGE)


def _phase(db):
    if not receipt_void_schema.is_ready():
        return "normal"
    phase = db.execute(select(rv.wallet_runtime_state.c.phase).where(
        rv.wallet_runtime_state.c.id == receipt_void_schema.SINGLETON_ID)).scalar_one_or_none()
    if phase not in ("normal", "enforced"):
        raise HTTPException(503, "wallet_policy_unavailable")
    return phase


def _account(db, user):
    account = db.execute(select(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id == user.id,
        rv.wallet_accounts.c.tombstoned_at.is_(None))).mappings().first()
    if account is None:
        raise HTTPException(503, "wallet_account_not_ready")
    return account


def can_purchase_telegram(db, telegram_id):
    """Preserve the existing anti-bypass lock on second-account creation."""
    if not telegram_id:
        return
    blocked = db.query(models.User).filter(
        models.User.telegram_id == telegram_id,
        models.User.purchases_blocked.is_(True)).first()
    if blocked is not None:
        _purchase_lock(blocked)


def can_purchase(db, user):
    _purchase_lock(user)
    can_purchase_telegram(db, user.telegram_id)
    if _phase(db) == "enforced":
        account = _account(db, user)
        debt = rv.wallet_debts
        remaining = db.execute(select(func.coalesce(func.sum(
            debt.c.amount - debt.c.adjusted_amount - debt.c.settled_amount), 0)).where(
                debt.c.customer_identity_id == account["customer_identity_id"],
                debt.c.state.in_(("open", "partially_settled")))).scalar_one()
        if remaining > 0:
            raise HTTPException(409, "open_debt")


def can_topup(db, user):
    if _phase(db) == "normal":
        # P9 will copy existing purchase locks to topup_blocked atomically.
        # Before that copy, removing this check would unlock current users.
        _purchase_lock(user)
        return
    account = _account(db, user)
    if account["topup_blocked"]:
        raise HTTPException(403, "topup_blocked")
