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


def minimum_cooperation_price(admin: models.AdminUser, quota_gb: float) -> Optional[int]:
    """The lowest cooperation price this admin may put on a package of this
    size, or None when there is no floor.

    Package.cooperation_price is what this Admin's own SELLERS pay when they
    provision with it (see unit_price: a Seller has no rate of their own, so
    it falls through to this field). The Admin sets it themselves - and
    nothing stopped them setting it below what the SAME package costs THEM,
    which means losing money on every seller sale, quietly, on every single
    one.

    So the floor is exactly the Admin's own cost: quota x their per-GB rate.
    Selling on at cost is allowed; selling at a loss is not, because it is
    almost always a typo or a misunderstanding of which of the two prices
    the field is.

    None when the superadmin has set no rate for this admin (there is no
    cost to compare against and inventing one would be guessing), and for a
    superadmin, who is the source of the prices rather than subject to them.
    """
    if admin is None or admin.is_superadmin:
        return None
    rate = int(getattr(admin, "wholesale_price_per_gb", 0) or 0)
    if rate <= 0:
        return None
    gb = float(quota_gb or 0)
    if gb <= 0:
        # No per-GB answer for an unlimited package. unit_price already
        # refuses to SELL one for a rate-based admin, which is the same
        # judgement made at the other end.
        return None
    return round(gb * rate)


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


def ensure_volume_available(admin: models.AdminUser) -> None:
    """A usage-billed reseller may not START a new sale with an empty pool.

    Reported 2026-09-12: "نه اصلا بهش حجم ندادم ولی تونست پکیج بسازه" - a
    reseller who had been given zero gigabytes could still sell, and their
    customer's traffic then drove the pool to -5 GB.

    There was simply no check. charge_for_package and charge_for_renewal
    both return immediately for billing_mode "usage" - correctly, since such
    an account is not charged a price at sale time - but "not charged here"
    silently became "not checked anywhere". A flat-priced reseller is
    refused the moment their balance cannot cover a sale (debit_admin's
    conditional UPDATE); the volume-billed one had no equivalent, so the
    whole point of handing out a GB pool was unenforced.

    The rule is "you must have volume to sell", not "you must hold the
    package's full size". Reserving the package size up front would turn
    usage billing into pre-paid billing, which is the opposite of what it
    is for - the reseller pays for what is CONSUMED, and how much of a
    20GB package a customer actually uses is unknown at sale time.

    What this deliberately does NOT do is cut off traffic already flowing.
    A pool that runs out mid-month keeps going negative (see
    AdminUser.volume_balance_gb and quota_manager._apply_delta) - that is
    debt, the same as a money balance inside its overdraft, and it is the
    superadmin's to collect. This only stops the reseller opening NEW
    business while they are already in the red.
    """
    if admin.is_superadmin or admin.billing_mode != "usage":
        return
    remaining = float(admin.volume_balance_gb or 0)
    if remaining > 0:
        return
    if remaining < 0:
        detail = (
            f"حجم شما تمام شده و {abs(remaining):g} گیگابایت هم بدهکار هستید - "
            "تا شارژ شدن حجم، فروش سرویس جدید ممکن نیست."
        )
    else:
        detail = (
            "حجم شما صفر است - برای فروش سرویس جدید باید ابتدا حجم دریافت کنید. "
            "حساب شما حجمی است، یعنی به‌جای کسر مبلغ در لحظه‌ی فروش، مصرف واقعی "
            "مشتریان از حجم شما کم می‌شود."
        )
    raise HTTPException(400, detail)


def require_package_to_grant(admin: models.AdminUser, package: Optional[models.Package]) -> None:
    """A reseller may only give a customer more quota or more time through a
    package. Refuses with the same message shape routers/users.py's
    create_user already uses for the same rule.

    Reported 2026-09-12: "وقتی یه یوزر یه یوزر میگیره می‌تونه اینجوری تمدید
    بزنه بدون هیچ گونه هزینه اضافی" - the panel's «تمدید این خرید» dialog
    asks for raw gigabytes and days with no package anywhere in it, so a
    reseller could extend a customer indefinitely for nothing. Three
    separate ways, all in that one dialog:

      * no package means charge_for_renewal below falls back to the
        account's per-GB rate, and an account with no rate set (the normal
        case for a flat-priced reseller) is charged zero;
      * add_days was never priced at all, at any rate - a year of extra
        time cost nothing even when a rate WAS set;
      * "مصرف قبلی صفر شود" hands back the whole quota, which is selling it
        again, and nothing looked at that checkbox on the cost side.

    Requiring a package closes all three at once and needs no new pricing
    rule, because a package already has a price for exactly this. It is the
    rule create_user has enforced since the credit system existed - a
    reseller creating a customer must pick a package - and a renewal is the
    same sale to the same customer.

    A superadmin is never charged for anything and keeps the raw fields:
    "give this customer three free days" is the panel owner's call to make.
    """
    if admin.is_superadmin or package is not None:
        return
    raise HTTPException(
        400,
        "تمدید بدون پکیج مجاز نیست - یک پکیج انتخاب کنید. "
        "حجم و روزِ دستی قیمتی ندارد که از اعتبار شما کم شود، "
        "پس تمدید باید از یکی از پکیج‌های تعریف‌شده انجام شود.",
    )


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
