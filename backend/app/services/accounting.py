"""The financial ledger behind the "حساب‌داری" (Accounting) section.

One entry point for WRITING (record() - called inline at every point money
moves, inside the caller's own transaction so the ledger row commits or
rolls back atomically with the event itself), one for the one-time
HISTORICAL import (backfill_if_needed() - called from main.py's
on_startup), and a set of role-scoped READ helpers used by
routers/accounting.py.

Role scoping follows services/hierarchy.py's 3-tier shape:
  superadmin  -> sees every entry (visible_admin_ids returns None)
  level-2     -> own entries + every one of their sellers'
  seller      -> only their own
An entry "belongs" to whoever LedgerEntry.admin_id points at (NULL = the
superadmin's own direct business - only the superadmin sees those)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models
from . import hierarchy

SALE_KINDS = ("sale_new", "sale_renew")
# Kinds a level-2 admin / seller is shown for their own tree. Expenses are
# panel-wide superadmin costs, never part of a reseller's books.
NON_SUPERADMIN_KINDS = ("sale_new", "sale_renew", "wallet_topup", "admin_credit_change", "admin_credit_spend", "admin_credit_refund")


def record(
    db: Session,
    kind: str,
    amount: int,
    *,
    user: Optional[models.User] = None,
    admin_id: Optional[int] = None,
    actor_admin_id: Optional[int] = None,
    package: Optional[models.Package] = None,
    purchase_id: Optional[int] = None,
    payment_card_id: Optional[int] = None,
    payment_method: Optional[str] = None,
    discount_code: Optional[str] = None,
    discount_amount: Optional[int] = None,
    category: Optional[str] = None,
    note: Optional[str] = None,
    created_at: Optional[dt.datetime] = None,
) -> models.LedgerEntry:
    """Adds a ledger row to the session WITHOUT committing - every call
    site already commits its own transaction right after, so the ledger
    row rides along atomically (a rolled-back sale never leaves a phantom
    income row behind).

    Snapshots (username/admin username/package name) are denormalized in
    on purpose so the books stay readable after the referenced row is
    deleted - same idea as Connection.package_name_snapshot."""
    admin_username = None
    if admin_id:
        row = db.get(models.AdminUser, admin_id)
        admin_username = row.username if row else None
    entry = models.LedgerEntry(
        kind=kind,
        amount=int(amount or 0),
        user_id=user.id if user else None,
        username_snapshot=user.username if user else None,
        admin_id=admin_id,
        admin_username_snapshot=admin_username,
        actor_admin_id=actor_admin_id,
        package_id=package.id if package else None,
        package_name_snapshot=package.name if package else None,
        purchase_id=purchase_id,
        payment_card_id=payment_card_id,
        payment_method=payment_method,
        discount_code=discount_code,
        discount_amount=discount_amount,
        category=category,
        note=note,
    )
    if created_at is not None:
        entry.created_at = created_at
    db.add(entry)
    return entry


def sale_fallback_price(db: Session, package: models.Package, owner_admin_id: Optional[int]) -> int:
    """Best-effort price for a sale when the caller didn't tell us the
    exact amount paid (e.g. a stale remote bot that predates the
    paid_amount field): the seller's own resale price when the customer
    belongs to a level-3 seller with a PackageSellerPrice row (mirrors
    routers/bot.py's list_packages), the plain customer price otherwise.
    Discounts can't be reconstructed here - the exact-amount path exists
    precisely so they don't have to be."""
    if owner_admin_id:
        row = (
            db.query(models.PackageSellerPrice)
            .filter(
                models.PackageSellerPrice.package_id == package.id,
                models.PackageSellerPrice.seller_admin_id == owner_admin_id,
            )
            .first()
        )
        if row is not None:
            return row.price or 0
    return package.price or 0


# ---------------------------------------------------------------- reading

def visible_admin_ids(db: Session, admin: models.AdminUser) -> Optional[list[Optional[int]]]:
    """None = unrestricted (superadmin). Otherwise the list of
    LedgerEntry.admin_id values this account may see - includes None never
    (NULL-owner rows are the superadmin's own business)."""
    if admin.is_superadmin:
        return None
    if hierarchy.is_seller(admin):
        return [admin.id]
    child_ids = [
        row[0]
        for row in db.query(models.AdminUser.id).filter(models.AdminUser.parent_admin_id == admin.id).all()
    ]
    return [admin.id, *child_ids]


def scoped_query(db: Session, admin: models.AdminUser):
    q = db.query(models.LedgerEntry)
    ids = visible_admin_ids(db, admin)
    if ids is not None:
        q = q.filter(models.LedgerEntry.admin_id.in_(ids))
        # Expenses are superadmin-only bookkeeping - even if one somehow
        # carried an admin_id, resellers have no business seeing costs.
        q = q.filter(models.LedgerEntry.kind != "expense")
    return q


def apply_filters(
    q,
    date_from: Optional[dt.datetime] = None,
    date_to: Optional[dt.datetime] = None,
    kind: Optional[str] = None,
    admin_id: Optional[int] = None,
    payment_card_id: Optional[int] = None,
):
    if date_from:
        q = q.filter(models.LedgerEntry.created_at >= date_from)
    if date_to:
        q = q.filter(models.LedgerEntry.created_at < date_to)
    if kind:
        q = q.filter(models.LedgerEntry.kind == kind)
    if admin_id is not None:
        q = q.filter(models.LedgerEntry.admin_id == admin_id)
    if payment_card_id is not None:
        q = q.filter(models.LedgerEntry.payment_card_id == payment_card_id)
    return q


def summary(db: Session, admin: models.AdminUser, date_from=None, date_to=None) -> dict:
    """Role-appropriate headline numbers + breakdowns, all computed off the
    same scoped/filtered base query so every number agrees with the
    transactions tab."""
    base = apply_filters(scoped_query(db, admin), date_from=date_from, date_to=date_to)

    totals = {
        kind: int(total or 0)
        for kind, total in base.with_entities(models.LedgerEntry.kind, func.sum(models.LedgerEntry.amount))
        .group_by(models.LedgerEntry.kind)
        .all()
    }
    sales_total = sum(totals.get(k, 0) for k in SALE_KINDS)

    out = {
        "totals": totals,
        "sales_total": sales_total,
        "wallet_topup_total": totals.get("wallet_topup", 0),
    }

    if admin.is_superadmin:
        expenses_total = totals.get("expense", 0)
        out["expenses_total"] = expenses_total
        out["net_profit"] = sales_total - expenses_total
        # Cash actually received on cards: card-paid sales + card top-ups
        # (wallet-paid sales are spending money that already arrived).
        card_cash = (
            base.filter(
                models.LedgerEntry.kind.in_((*SALE_KINDS, "wallet_topup")),
                models.LedgerEntry.payment_method != "wallet",
            )
            .with_entities(func.sum(models.LedgerEntry.amount))
            .scalar()
        )
        out["card_cash_total"] = int(card_cash or 0)
    else:
        # A reseller's cost of goods is what their credit was debited at
        # (cooperation price), minus rolled-back charges.
        cost = totals.get("admin_credit_spend", 0) - totals.get("admin_credit_refund", 0)
        out["credit_spent_total"] = cost
        out["margin_total"] = sales_total - cost if sales_total else None
        out["credit_balance"] = admin.balance or 0

    # Breakdown by admin (superadmin: every admin; level-2: their sellers).
    if not hierarchy.is_seller(admin):
        rows = (
            base.filter(models.LedgerEntry.kind.in_(SALE_KINDS))
            .with_entities(
                models.LedgerEntry.admin_id,
                models.LedgerEntry.admin_username_snapshot,
                func.sum(models.LedgerEntry.amount),
                func.count(models.LedgerEntry.id),
            )
            .group_by(models.LedgerEntry.admin_id, models.LedgerEntry.admin_username_snapshot)
            .order_by(func.sum(models.LedgerEntry.amount).desc())
            .all()
        )
        out["by_admin"] = [
            {"admin_id": aid, "admin_username": name, "sales_total": int(total or 0), "sales_count": count}
            for aid, name, total, count in rows
        ]

    if admin.is_superadmin:
        rows = (
            base.filter(models.LedgerEntry.payment_card_id.isnot(None))
            .with_entities(
                models.LedgerEntry.payment_card_id,
                func.sum(models.LedgerEntry.amount),
                func.count(models.LedgerEntry.id),
            )
            .group_by(models.LedgerEntry.payment_card_id)
            .all()
        )
        cards = {c.id: c for c in db.query(models.PaymentCard).all()}
        out["by_card"] = [
            {
                "payment_card_id": cid,
                "card_number": cards[cid].card_number if cid in cards else None,
                "card_holder": cards[cid].card_holder if cid in cards else None,
                "total": int(total or 0),
                "count": count,
            }
            for cid, total, count in rows
        ]

    return out


def series(db: Session, admin: models.AdminUser, granularity: str = "day", date_from=None, date_to=None) -> list[dict]:
    """Daily (or monthly) income/expense series for the charts. Grouped by
    func.date() which both SQLite and MySQL implement; months are rolled up
    from the daily rows in Python instead of dialect-specific date
    formatting (see services/backup.py for the same
    keep-it-dialect-portable philosophy)."""
    if date_from is None:
        date_from = dt.datetime.utcnow() - dt.timedelta(days=30 if granularity == "day" else 365)
    base = apply_filters(scoped_query(db, admin), date_from=date_from, date_to=date_to)
    rows = (
        base.with_entities(
            func.date(models.LedgerEntry.created_at),
            models.LedgerEntry.kind,
            func.sum(models.LedgerEntry.amount),
        )
        .group_by(func.date(models.LedgerEntry.created_at), models.LedgerEntry.kind)
        .all()
    )
    buckets: dict[str, dict] = {}
    for day, kind, total in rows:
        day = str(day)
        key = day[:7] if granularity == "month" else day
        b = buckets.setdefault(key, {"period": key, "sales": 0, "expenses": 0, "wallet_topup": 0})
        if kind in SALE_KINDS:
            b["sales"] += int(total or 0)
        elif kind == "expense":
            b["expenses"] += int(total or 0)
        elif kind == "wallet_topup":
            b["wallet_topup"] += int(total or 0)
    return [buckets[k] for k in sorted(buckets)]


def subtree_rollup(db: Session, admin: models.AdminUser, date_from=None, date_to=None) -> list[dict]:
    """One row per direct sub-account: what they sold, what they hold, how
    many customers they carry.

    The aggregate half of "record vs aggregate visibility". Record access is
    deliberately NOT removed - a level-2 Admin funds their Sellers out of
    their own balance and is therefore answerable for those sales, and an
    Admin who cannot see a vanished Seller's customers cannot serve them.
    What was missing is the other view: managing Sellers as accounts rather
    than by scrolling a merged customer list.

    Computed with grouped queries rather than a loop per child. The loop
    version is the obvious one and costs four round trips per Seller, which
    on this panel's tree is most of the page's latency for numbers that
    could all be fetched at once.

    Root accounts are included for a superadmin even though their
    parent_admin_id is NULL: this panel's real tree has four roots, and a
    report that silently omitted them would be worse than no report.
    """
    if hierarchy.is_seller(admin):
        return []

    children = db.query(models.AdminUser).filter(
        models.AdminUser.parent_admin_id == admin.id,
        models.AdminUser.is_superadmin.is_(False),
    ).all()
    if admin.is_superadmin:
        seen = {c.id for c in children}
        children += [
            row for row in db.query(models.AdminUser).filter(
                models.AdminUser.parent_admin_id.is_(None),
                models.AdminUser.is_superadmin.is_(False),
            ).all()
            if row.id not in seen
        ]
    if not children:
        return []

    ids = [c.id for c in children]

    customers: dict[int, int] = {}
    active: dict[int, int] = {}
    for owner_id, status, count in (
        db.query(models.User.owner_admin_id, models.User.status, func.count(models.User.id))
        .filter(models.User.owner_admin_id.in_(ids))
        .group_by(models.User.owner_admin_id, models.User.status)
        .all()
    ):
        customers[owner_id] = customers.get(owner_id, 0) + int(count or 0)
        if status == models.UserStatus.active:
            active[owner_id] = active.get(owner_id, 0) + int(count or 0)

    sales_q = apply_filters(
        db.query(models.LedgerEntry).filter(
            models.LedgerEntry.admin_id.in_(ids),
            models.LedgerEntry.kind.in_(SALE_KINDS),
        ),
        date_from=date_from, date_to=date_to,
    )
    sales: dict[int, tuple[int, int]] = {
        admin_id: (int(total or 0), int(count or 0))
        for admin_id, total, count in sales_q.with_entities(
            models.LedgerEntry.admin_id,
            func.sum(models.LedgerEntry.amount),
            func.count(models.LedgerEntry.id),
        ).group_by(models.LedgerEntry.admin_id).all()
    }

    out = []
    for child in sorted(children, key=lambda c: c.username.lower()):
        total, count = sales.get(child.id, (0, 0))
        balance = child.balance or 0
        out.append({
            "id": child.id,
            "username": child.username,
            "role": hierarchy.role(child),
            "customers": customers.get(child.id, 0),
            "active_customers": active.get(child.id, 0),
            "sales_total": total,
            "sales_count": count,
            "balance": balance,
            "credit_limit": child.credit_limit or 0,
            # Surfaced separately rather than left for the reader to notice
            # from a minus sign - being in debt is the one thing on this row
            # that needs acting on.
            "in_debt": balance < 0,
            "volume_balance_gb": child.volume_balance_gb or 0,
            "billing_mode": child.billing_mode or "flat",
        })
    return out


# --------------------------------------------------------------- backfill

def backfill_if_needed(db: Session) -> int:
    """One-time import of pre-existing financial history so the section
    isn't empty on day one (decided with the panel owner 2026-08-08 - see
    docs/accounting-design.md): every Purchase becomes a sale_new row at
    its historical price/date, every AdminBalanceLog an
    admin_credit_change. Guarded by PanelSettings.accounting_backfilled so
    it can only ever run once. Returns how many rows were imported."""
    settings = db.query(models.PanelSettings).first()
    if settings is None or settings.accounting_backfilled:
        return 0

    # Historical discounts: DiscountCodeRedemption is the only record of
    # what a customer REALLY paid pre-ledger (package_price - discount) -
    # match each redemption to the nearest same-user purchase within 15
    # minutes (both flows redeem the code within seconds of the purchase
    # call, in either order) so a discounted historical purchase isn't
    # imported at its full list price. Each redemption matches at most
    # once (popped) so two same-day purchases can't both claim it.
    unmatched_redemptions: dict[int, list] = {}
    for red in db.query(models.DiscountCodeRedemption).all():
        if red.user_id and red.created_at:
            unmatched_redemptions.setdefault(red.user_id, []).append(red)

    imported = 0
    purchases = (
        db.query(models.Purchase)
        .options(joinedload(models.Purchase.user), joinedload(models.Purchase.package))
        .all()
    )
    for purchase in purchases:
        user = purchase.user
        owner_admin_id = user.owner_admin_id if user else None
        package = purchase.package
        amount = sale_fallback_price(db, package, owner_admin_id) if package else 0
        discount_code = None
        discount_amount = None
        candidates = unmatched_redemptions.get(user.id if user else None) or []
        for red in candidates:
            if purchase.created_at and abs((red.created_at - purchase.created_at).total_seconds()) <= 900:
                base_price = red.package_price if red.package_price is not None else amount
                discount_amount = red.discount_amount or 0
                amount = max(0, base_price - discount_amount)
                discount_code = red.code.code if red.code else None
                candidates.remove(red)
                break
        record(
            db,
            "sale_new",
            amount,
            user=user,
            admin_id=owner_admin_id,
            package=package,
            purchase_id=purchase.id,
            discount_code=discount_code,
            discount_amount=discount_amount,
            note="ثبت تاریخی (backfill)",
            created_at=purchase.created_at,
        )
        imported += 1

    for log in db.query(models.AdminBalanceLog).all():
        record(
            db,
            "admin_credit_change",
            log.amount,
            admin_id=log.admin_id,
            actor_admin_id=log.created_by_id,
            note=(log.note or None) or "ثبت تاریخی (backfill)",
            created_at=log.created_at,
        )
        imported += 1

    settings.accounting_backfilled = True
    db.commit()
    return imported
