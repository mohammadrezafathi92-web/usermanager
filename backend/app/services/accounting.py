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

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from .. import models
from . import hierarchy, jalali

SALE_KINDS = ("sale_new", "sale_renew")
# Kinds a level-2 admin / seller is shown for their own tree. Expenses are
# panel-wide superadmin costs, never part of a reseller's books.
NON_SUPERADMIN_KINDS = (
    "sale_new", "sale_renew", "wallet_topup",
    "admin_credit_change", "admin_credit_spend", "admin_credit_refund",
    "admin_usage_charge", "admin_payment",
)

# What a reseller owes moves by these kinds (see models.LedgerEntry's
# docstring for why admin_credit_spend is deliberately absent).
RECEIVABLE_DEBIT_KINDS = ("admin_credit_change", "admin_usage_charge")
PAYMENT_KIND = "admin_payment"


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
        # Survives the admin being deleted, unlike admin_id above - see
        # models.LedgerEntry.owner_admin_id_snapshot.
        owner_admin_id_snapshot=admin_id,
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


def record_panel_sale(
    db: Session,
    kind: str,
    user: models.User,
    package: Optional[models.Package],
    *,
    purchase_id: Optional[int] = None,
    actor_admin_id: Optional[int] = None,
) -> Optional[models.LedgerEntry]:
    """Books the revenue side of a sale made from the WEB PANEL.

    BUG FIXED 2026-09: sale_new/sale_renew were only ever recorded by
    routers/bot.py. Selling the same package from the panel wrote the COST
    side (the reseller's credit being debited, see
    services/admin_billing.py) and no income at all - so "فروش", "سود
    خالص", the chart and the per-admin breakdown silently excluded every
    panel-made sale, and a reseller who works mostly from the panel was
    shown a permanent loss: costs rising against zero revenue.

    The panel never asks what the customer handed over, so the price is
    taken from the package the same way a sale with no stated amount
    already was for the bot (sale_fallback_price: the seller's own resale
    price when they have set one, the list price otherwise).

    payment_method is left NULL rather than guessed - the panel genuinely
    does not know whether the customer paid cash, card or anything else.
    """
    if package is None:
        return None  # a package-less admin-created user isn't a sale
    amount = sale_fallback_price(db, package, user.owner_admin_id)
    if amount <= 0:
        return None
    return record(
        db, kind, amount,
        user=user,
        admin_id=user.owner_admin_id,
        actor_admin_id=actor_admin_id,
        package=package,
        purchase_id=purchase_id,
        note="فروش از پنل",
    )


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


def owner_column():
    """Which column decides who a ledger row belongs to.

    owner_admin_id_snapshot, not admin_id: the latter is a foreign key with
    ondelete="SET NULL", and a NULL admin_id means "the superadmin's own
    direct business" here - so deleting a reseller used to hand their whole
    sales history to the superadmin and silently rewrite past reports (bug
    found 2026-09). The snapshot is a plain integer nothing cascades over.

    coalesce keeps rows written before that column existed working: they
    have no snapshot, so their admin_id is used exactly as before.
    """
    return func.coalesce(models.LedgerEntry.owner_admin_id_snapshot, models.LedgerEntry.admin_id)


def scoped_query(db: Session, admin: models.AdminUser):
    q = db.query(models.LedgerEntry)
    ids = visible_admin_ids(db, admin)
    if ids is not None:
        q = q.filter(owner_column().in_(ids))
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


def _receivable_expr():
    """A signed amount per row for the reseller current-account: debits
    positive (credit granted, metered usage), payments negative."""
    return sa.case(
        (models.LedgerEntry.kind == PAYMENT_KIND, -models.LedgerEntry.amount),
        else_=models.LedgerEntry.amount,
    )


def receivables_start(db: Session):
    """When the current-account started counting - see
    models.PanelSettings.receivables_start_at."""
    settings = db.query(models.PanelSettings).first()
    return settings.receivables_start_at if settings else None


def _receivable_base(db: Session, date_to=None):
    q = db.query(models.LedgerEntry).filter(
        models.LedgerEntry.kind.in_((*RECEIVABLE_DEBIT_KINDS, PAYMENT_KIND))
    )
    # Everything before the line the panel owner drew is left in the ledger
    # but not counted as outstanding.
    start = receivables_start(db)
    if start is not None:
        q = q.filter(models.LedgerEntry.created_at >= start)
    # A balance is a running total, so it is never bounded from BELOW by
    # the report's date_from - what someone owes today includes what they
    # already owed before the period started. Only an upper bound ("as of")
    # makes sense here.
    if date_to:
        q = q.filter(models.LedgerEntry.created_at < date_to)
    return q


def receivables_for_admin(db: Session, admin_id: int, date_to=None) -> int:
    """How much this one reseller still owes - see models.LedgerEntry's
    docstring for the formula and why admin_credit_spend is excluded."""
    total = (
        _receivable_base(db, date_to)
        .filter(owner_column() == admin_id)
        .with_entities(func.sum(_receivable_expr()))
        .scalar()
    )
    return int(total or 0)


def receivables_total(db: Session, admin: models.AdminUser, date_to=None) -> int:
    """Everything every visible reseller still owes, added up.

    Derived from the same rows the list shows (deleted accounts excluded)
    so the headline figure can never disagree with the rows under it.
    """
    return sum(r["owed"] for r in receivables_by_admin(db, admin, date_to=date_to))


def receivables_by_admin(db: Session, admin: models.AdminUser, date_to=None) -> list[dict]:
    """One row per reseller who has ever been granted credit or metered -
    what they were charged, what they paid, what is left. Drives the
    "طلب از نماینده‌ها" list, which is where payments get recorded."""
    q = _receivable_base(db, date_to)
    ids = visible_admin_ids(db, admin)
    if ids is not None:
        q = q.filter(owner_column().in_(ids))

    rows = (
        q.with_entities(
            owner_column().label("owner_id"),
            models.LedgerEntry.kind,
            func.sum(models.LedgerEntry.amount),
        )
        .group_by(owner_column(), models.LedgerEntry.kind)
        .all()
    )
    per: dict[int, dict] = {}
    for owner_id, kind, total in rows:
        if owner_id is None:
            continue  # the superadmin's own business owes itself nothing
        bucket = per.setdefault(owner_id, {"charged": 0, "paid": 0})
        if kind == PAYMENT_KIND:
            bucket["paid"] += int(total or 0)
        else:
            bucket["charged"] += int(total or 0)

    if not per:
        return []
    admins = {
        a.id: a for a in db.query(models.AdminUser).filter(models.AdminUser.id.in_(list(per))).all()
    }

    out = []
    for owner_id, bucket in per.items():
        row = admins.get(owner_id)
        # A deleted account is dropped rather than listed: there is nobody
        # left to collect from, the "ثبت دریافت" action 404s against a
        # missing admin anyway (so the row was a dead end), and a list full
        # of closed accounts buries the ones that can actually be acted on.
        # Their ledger rows stay exactly where they are - this only decides
        # what the collections list shows.
        if row is None:
            continue
        out.append({
            "admin_id": owner_id,
            "username": row.username,
            "deleted": False,
            "charged_total": bucket["charged"],
            "paid_total": bucket["paid"],
            "owed": bucket["charged"] - bucket["paid"],
            "billing_mode": row.billing_mode or "flat",
        })
    out.sort(key=lambda r: r["owed"], reverse=True)
    return out


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

        # Whose sales are whose. sales_total above is every sale on the
        # panel, which is NOT the superadmin's income: a reseller's retail
        # sale is money that goes to the RESELLER. The superadmin earns
        # from their own direct customers plus what resellers actually pay
        # them (see below), so the two are reported separately instead of
        # being added together into one misleading "فروش" figure
        # (bug found 2026-09).
        own_sales = int(
            base.filter(
                models.LedgerEntry.kind.in_(SALE_KINDS),
                sa.or_(owner_column().is_(None), owner_column() == admin.id),
            ).with_entities(func.sum(models.LedgerEntry.amount)).scalar() or 0
        )
        out["own_sales_total"] = own_sales
        out["reseller_sales_total"] = sales_total - own_sales

        # Cash actually collected FROM resellers - the superadmin's real
        # income from the reseller side of the business. Granting credit is
        # not income; it is frequently handed over before it is paid for.
        payments_in = totals.get(PAYMENT_KIND, 0)
        out["admin_payments_total"] = payments_in
        out["net_profit"] = own_sales + payments_in - expenses_total

        # Cash actually received on cards: card-paid sales + card top-ups
        # (wallet-paid sales are spending money that already arrived).
        #
        # `!= "wallet"` alone silently dropped every row whose
        # payment_method was never recorded (older bot builds, backfilled
        # history): in SQL, NULL != 'wallet' is NULL, not true. Those rows
        # are cash unless proven otherwise, so they are counted.
        not_wallet = sa.or_(
            models.LedgerEntry.payment_method.is_(None),
            models.LedgerEntry.payment_method != "wallet",
        )
        card_cash = (
            base.filter(models.LedgerEntry.kind.in_((*SALE_KINDS, "wallet_topup")), not_wallet)
            .with_entities(func.sum(models.LedgerEntry.amount))
            .scalar()
        )
        out["card_cash_total"] = int(card_cash or 0)
        out["receivables_total"] = receivables_total(db, admin, date_to=date_to)
    else:
        # A reseller's cost of goods: credit debited at cooperation price
        # (flat mode) or metered traffic priced per GB (usage mode), minus
        # rolled-back charges.
        cost = (
            totals.get("admin_credit_spend", 0)
            + totals.get("admin_usage_charge", 0)
            - totals.get("admin_credit_refund", 0)
        )
        out["credit_spent_total"] = cost
        # Showing nothing when sales are zero hid a real loss: an account
        # that spent credit and sold nothing has a negative margin, not an
        # unknown one. Only a completely empty period has no answer.
        out["margin_total"] = None if (sales_total == 0 and cost == 0) else sales_total - cost
        # A usage-billed reseller holds GB, not tomans - reporting
        # admin.balance for them showed a flat 0 next to a real GB pool.
        if (admin.billing_mode or "flat") == "usage":
            out["billing_mode"] = "usage"
            out["volume_balance_gb"] = admin.volume_balance_gb or 0
            out["credit_balance"] = None
        else:
            out["billing_mode"] = "flat"
            out["credit_balance"] = admin.balance or 0
        out["owed_total"] = receivables_for_admin(db, admin.id, date_to=date_to)

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
        # Anchored to date_to when one was given, not to "now": asking for
        # everything up to the end of last month used to return an empty
        # chart, because the default window started 30 days before TODAY,
        # which is after the requested end.
        anchor = date_to or dt.datetime.utcnow()
        date_from = anchor - dt.timedelta(days=30 if granularity == "day" else 365)
    base = apply_filters(scoped_query(db, admin), date_from=date_from, date_to=date_to)
    # Bucket by the LOCAL calendar day, not the UTC one. created_at is
    # stored in UTC; at +03:30 everything sold after 20:30 UTC belongs to
    # the next day in Tehran, so grouping on the raw timestamp put the
    # evening's sales on the wrong bar (routers/dashboard.py already
    # applies this same offset for its today/this-month figures - the
    # chart simply never did).
    #
    # Bucketed in Python rather than with a shifted GROUP BY because
    # timestamp arithmetic is exactly the kind of thing SQLite and MySQL
    # spell differently (same reasoning as this function's original
    # comment about avoiding dialect-specific date formatting). The window
    # is always bounded - a month by default - so this reads a few
    # thousand rows at most, not the whole ledger.
    offset = dt.timedelta(minutes=jalali.get_display_offset())
    rows = base.with_entities(
        models.LedgerEntry.created_at,
        models.LedgerEntry.kind,
        models.LedgerEntry.amount,
    ).all()

    buckets: dict[str, dict] = {}
    for created_at, kind, amount in rows:
        if created_at is None:
            continue
        day = (created_at + offset).strftime("%Y-%m-%d")
        key = day[:7] if granularity == "month" else day
        b = buckets.setdefault(key, {"period": key, "sales": 0, "expenses": 0, "wallet_topup": 0})
        if kind in SALE_KINDS:
            b["sales"] += int(amount or 0)
        elif kind == "expense":
            b["expenses"] += int(amount or 0)
        elif kind == "wallet_topup":
            b["wallet_topup"] += int(amount or 0)
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

    # A ROLLUP row has to cover the whole branch under that account, not
    # just the account itself.
    #
    # BUG FIXED 2026-09 ("تب زیرمجموعه‌های من درست کار نمی‌کنه"): every
    # figure below was counted for the child alone. A level-2 Admin whose
    # business actually runs through their own Sellers therefore showed
    # near-zero customers and near-zero sales - the numbers were all real,
    # they were just sitting one level further down where nothing looked.
    # Each child now aggregates over itself PLUS its own children.
    branch: dict[int, list[int]] = {}
    for child in children:
        own_children = [
            row[0] for row in db.query(models.AdminUser.id)
            .filter(models.AdminUser.parent_admin_id == child.id).all()
        ]
        branch[child.id] = [child.id, *own_children]
    all_ids = sorted({aid for members in branch.values() for aid in members})

    per_admin_customers: dict[int, int] = {}
    per_admin_active: dict[int, int] = {}
    for owner_id, status, count in (
        db.query(models.User.owner_admin_id, models.User.status, func.count(models.User.id))
        .filter(models.User.owner_admin_id.in_(all_ids))
        .group_by(models.User.owner_admin_id, models.User.status)
        .all()
    ):
        per_admin_customers[owner_id] = per_admin_customers.get(owner_id, 0) + int(count or 0)
        if status == models.UserStatus.active:
            per_admin_active[owner_id] = per_admin_active.get(owner_id, 0) + int(count or 0)

    sales_q = apply_filters(
        db.query(models.LedgerEntry).filter(
            owner_column().in_(all_ids),
            models.LedgerEntry.kind.in_(SALE_KINDS),
        ),
        date_from=date_from, date_to=date_to,
    )
    per_admin_sales: dict[int, tuple[int, int]] = {
        owner_id: (int(total or 0), int(count or 0))
        for owner_id, total, count in sales_q.with_entities(
            owner_column(),
            func.sum(models.LedgerEntry.amount),
            func.count(models.LedgerEntry.id),
        ).group_by(owner_column()).all()
    }

    out = []
    for child in sorted(children, key=lambda c: c.username.lower()):
        members = branch[child.id]
        customers = sum(per_admin_customers.get(i, 0) for i in members)
        active = sum(per_admin_active.get(i, 0) for i in members)
        total = sum(per_admin_sales.get(i, (0, 0))[0] for i in members)
        count = sum(per_admin_sales.get(i, (0, 0))[1] for i in members)
        balance = child.balance or 0
        volume_gb = child.volume_balance_gb or 0
        usage = (child.billing_mode or "flat") == "usage"
        out.append({
            "id": child.id,
            "username": child.username,
            "role": hierarchy.role(child),
            "customers": customers,
            "active_customers": active,
            "sales_total": total,
            "sales_count": count,
            # How much of the branch is the child's own vs. their sellers' -
            # without this the rollup hides whether an Admin sells at all.
            "own_customers": per_admin_customers.get(child.id, 0),
            "own_sales_total": per_admin_sales.get(child.id, (0, 0))[0],
            "sub_accounts": len(members) - 1,
            "balance": balance,
            "credit_limit": child.credit_limit or 0,
            # Surfaced separately rather than left for the reader to notice
            # from a minus sign - being in debt is the one thing on this row
            # that needs acting on.
            #
            # Read off whichever pool actually governs this account: a
            # usage-billed reseller holds GB, and their toman balance is a
            # frozen leftover from before they were switched over, so
            # judging them by it reported a healthy credit for an account
            # that has none and missed one that had over-consumed its GB
            # (reported 2026-09).
            "in_debt": (volume_gb < 0) if usage else (balance < 0),
            "volume_balance_gb": volume_gb,
            "billing_mode": "usage" if usage else "flat",
            # What they owe YOU (granted credit + metered usage - payments
            # received), which is a different question from the prepaid
            # balance they still hold.
            "owed": receivables_for_admin(db, child.id, date_to=date_to),
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
