"""What a level-2 Admin or Seller owes the panel, and taking it.

Lives in services/ rather than in routers/users.py because BOTH the panel
and the sales bot sell: a customer buying through a reseller's own Telegram
bot is the same transaction as an admin creating them from the panel, and
the reseller owes the same amount either way. While these functions lived
next to one router, only that router charged - which is exactly how every
bot sale came to be free.

The public names are unprefixed; the leading underscores they carried as
private helpers of one module would be wrong for a shared service.
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from . import accounting


def unit_price(admin: models.AdminUser, package: models.Package) -> int:
    """What ONE unit of this package costs THIS admin.

    Shared by the charge and the refund below. They used to compute it
    separately with the same expression, which is a drift waiting to happen
    - and the half that drifts is the refund, so an admin would be charged
    one number and given back another.

    A per-GB rate on the account (AdminUser.wholesale_price_per_gb) wins
    when it is set. That is the point of the rate: the Admin builds and
    prices their own packages, so Package.cooperation_price is a number
    they choose for themselves, and using it to decide what they owe the
    superadmin meant the credit system metered nothing. quota_gb x rate is
    set by the superadmin and cannot be edited from the Admin's side.

    An UNLIMITED package (quota_gb = 0) has no per-GB answer. It is refused
    rather than silently charged nothing - a rate-based admin who could
    make everything unlimited would be back to paying zero, which is the
    hole this closes. The superadmin can still allow it deliberately by
    clearing that admin's rate.
    """
    rate = int(getattr(admin, "wholesale_price_per_gb", 0) or 0)
    if rate <= 0:
        return package.cooperation_price if package.cooperation_price is not None else (package.price or 0)

    quota_gb = float(package.quota_gb or 0)
    if quota_gb <= 0:
        raise HTTPException(
            400,
            f"برای «{admin.username}» نرخ گیگی ({rate:,} تومان) تعیین شده و بسته‌ی نامحدود "
            f"«{package.name}» با آن قابل محاسبه نیست - یا حجم بسته را مشخص کنید یا نرخ گیگی این ادمین را بردارید",
        )
    return round(quota_gb * rate)


def charge_for_package(db: Session, admin: models.AdminUser, package: models.Package, units: int = 1) -> None:
    """Atomically deducts `units` times the package's wholesale price (its
    cooperation_price, or the regular customer price if no cooperation
    price is configured) from a non-superadmin admin's own credit balance -
    what it costs them to provision this package for their own group.
    Superadmins own everything outright and are never charged. Uses a
    single conditional UPDATE (`WHERE balance >= cost`), same pattern as
    the customer wallet debit in routers/bot.py's add_balance, so two
    concurrent bulk-creates from the same admin can't both succeed past
    their real balance. Raises HTTPException(400) - and deducts nothing -
    if the balance can't cover it."""
    if admin.is_superadmin or units <= 0:
        return
    if admin.billing_mode == "usage":
        # این ادمین بابت هر پکیج پول کم نمی‌شود - اعتبارش به‌صورت حجمی
        # (volume_balance_gb) و لحظه‌ای در quota_manager.py's _apply_delta
        # کسر می‌شود، نه یکجا در لحظه ساخت کاربر.
        return
    cost = unit_price(admin, package) * units
    debit_admin(
        db, admin, cost, package=package,
        note=f"{units} × {package.name}" if units > 1 else None,
    )


def debit_admin(
    db: Session, admin: models.AdminUser, cost: int, *,
    package: Optional[models.Package] = None, note: Optional[str] = None,
    what: str = "این پکیج",
) -> None:
    """Takes `cost` from the admin's credit, or refuses and takes nothing.

    Extracted so buying and renewing debit through the same code. They were
    about to be two copies of the overdraft comparison, the atomic UPDATE
    and the ledger write - and the copy that drifts is the one nobody is
    watching, which for money means an admin charged by one rule and
    refunded by another.
    """
    if cost <= 0:
        return
    # The floor is -credit_limit, not zero (see AdminUser.credit_limit).
    # Still evaluated inside the UPDATE's WHERE rather than in Python, so
    # two concurrent sales from the same account cannot both pass a check
    # that only one of them could really afford.
    limit = int(getattr(admin, "credit_limit", 0) or 0)
    result = db.execute(
        models.AdminUser.__table__.update()
        .where(models.AdminUser.id == admin.id, models.AdminUser.balance - cost >= -limit)
        .values(balance=models.AdminUser.balance - cost)
    )
    if result.rowcount == 0:
        db.commit()
        available = (admin.balance or 0) + limit
        msg = f"اعتبار شما کافی نیست - {what} {cost:,} تومان از اعتبار شما کم می‌کند"
        if limit:
            # Naming the overdraft matters: without it the message claims a
            # hard limit that is not the one actually being applied.
            msg += f" و با احتساب سقف بدهی {limit:,} تومان، فقط {available:,} تومان در دسترس دارید"
        raise HTTPException(400, msg)
    # Accounting: the reseller's cost of goods (see services/accounting.py) -
    # committed together with the deduction itself.
    accounting.record(
        db, "admin_credit_spend", cost,
        admin_id=admin.id, actor_admin_id=admin.id, package=package,
        payment_method="admin_credit", note=note,
    )
    db.commit()


def charge_for_renewal(
    db: Session, admin: models.AdminUser, package: Optional[models.Package], add_gb: float,
) -> None:
    """A renewal costs the admin too. It never used to.

    Renewals were completely free from the credit system's point of view -
    only creating a customer and adding a package were charged. On a panel
    whose customers mostly renew, that is most of the revenue passing
    through unmetered.

    Two shapes, because a renewal has two:
      - with a package, it is that package being sold again, so it costs
        exactly what selling it costs;
      - with raw gigabytes, it costs add_gb x the account's per-GB rate.

    Raw gigabytes with NO rate set are still free, and deliberately so:
    there is no package to take a price from and inventing one would be
    guessing at the operator's own pricing. Set a per-GB rate on the
    account (AdminUser.wholesale_price_per_gb) and it is metered.
    """
    if admin.is_superadmin or admin.billing_mode == "usage":
        return
    if package is not None:
        charge_for_package(db, admin, package, units=1)
        return

    rate = int(getattr(admin, "wholesale_price_per_gb", 0) or 0)
    if rate <= 0 or add_gb <= 0:
        return
    debit_admin(
        db, admin, round(add_gb * rate),
        note=f"تمدید {add_gb:g} گیگابایت", what="این تمدید",
    )


def refund_for_package(db: Session, admin: models.AdminUser, package: models.Package, units: int) -> None:
    """Gives back credit reserved by charge_for_package for users
    that ended up NOT being created (e.g. bulk-create hit its collision
    safety cap before reaching the requested count) - see bulk_create_users
    below."""
    if admin.is_superadmin or units <= 0:
        return
    if admin.billing_mode == "usage":
        return
    amount = unit_price(admin, package) * units
    if amount <= 0:
        return
    db.execute(
        models.AdminUser.__table__.update()
        .where(models.AdminUser.id == admin.id)
        .values(balance=models.AdminUser.balance + amount)
    )
    accounting.record(
        db, "admin_credit_refund", amount,
        admin_id=admin.id, actor_admin_id=admin.id, package=package,
        payment_method="admin_credit",
        note=f"بازگشت اعتبار {units} پکیج ساخته‌نشده",
    )
    db.commit()
