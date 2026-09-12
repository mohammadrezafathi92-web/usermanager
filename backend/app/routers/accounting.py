"""The "حساب‌داری" (Accounting) section's API - thin role-scoped reads over
services/accounting.py's ledger plus superadmin-only manual expense entry.
Design agreed with the panel owner 2026-08-08 (docs/accounting-design.md):
superadmin sees the whole panel (incl. expenses + net profit), a level-2
Admin sees their own tree (self + their sellers), a Seller only themselves -
all enforced by accounting.scoped_query(), never by the frontend."""
import datetime as dt
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import jalali
from ..services.jalali import fmt_jalali
from ..deps import get_current_admin, require_superadmin, require_confirm_password, require_permission
from ..services import accounting, hierarchy

# Every endpoint here is already scoped by role (a Seller only ever sees
# their own rows). The permission decides whether they see the section at
# all - a level-2 Admin may not want a sub-seller studying figures. Passes
# automatically for superadmins and level-2 Admins.
router = APIRouter(
    prefix="/api/accounting", tags=["accounting"],
    dependencies=[Depends(require_permission("view_accounting"))],
)


def _parse_date(value: Optional[str], end: bool = False) -> Optional[dt.datetime]:
    """YYYY-MM-DD (a LOCAL calendar day) -> the UTC instant it starts at;
    `end` dates become exclusive midnight-after so a single-day range
    [d, d] covers that whole day.

    BUG FIXED 2026-09: the date picked in the panel is a local (Jalali)
    day, but it used to be compared straight against created_at, which is
    stored in UTC - so every boundary sat 3.5 hours off in Tehran and each
    end of the range pulled in (or dropped) an evening's transactions.
    routers/dashboard.py already converted local midnight back to UTC for
    its today/this-month figures; the accounting filters simply never did,
    which is why the two pages could disagree about the same day.
    """
    if not value:
        return None
    try:
        parsed = dt.datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, "فرمت تاریخ باید YYYY-MM-DD باشد")
    if end:
        parsed += dt.timedelta(days=1)
    return parsed - dt.timedelta(minutes=jalali.get_display_offset())


@router.get("/summary")
def get_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    out = accounting.summary(
        db, current,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to, end=True),
    )
    out["role"] = hierarchy.role(current)
    return out


@router.get("/subtree")
def get_subtree_rollup(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    """One row per direct sub-account - see accounting.subtree_rollup.

    Returns an empty list rather than 403 for a Seller, who simply has no
    sub-accounts. A 403 would say "you are not allowed", which is not true
    and would make the frontend show an error where there is only nothing
    to show.
    """
    return accounting.subtree_rollup(
        db, current,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to, end=True),
    )


@router.get("/series")
def get_series(
    granularity: str = "day",
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    if granularity not in ("day", "month"):
        raise HTTPException(400, "granularity باید day یا month باشد")
    return accounting.series(
        db, current, granularity,
        date_from=_parse_date(date_from), date_to=_parse_date(date_to, end=True),
    )


@router.get("/transactions", response_model=schemas.LedgerPage)
def list_transactions(
    page: int = 1,
    page_size: int = 50,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kind: Optional[str] = None,
    admin_id: Optional[int] = None,
    payment_card_id: Optional[int] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    q = accounting.apply_filters(
        accounting.scoped_query(db, current),
        date_from=_parse_date(date_from), date_to=_parse_date(date_to, end=True),
        kind=kind, admin_id=admin_id, payment_card_id=payment_card_id,
    )
    total = q.count()
    items = (
        q.order_by(models.LedgerEntry.created_at.desc(), models.LedgerEntry.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return schemas.LedgerPage(
        items=[schemas.LedgerEntryOut.model_validate(e) for e in items],
        total=total, page=page, page_size=page_size,
    )


@router.post("/expenses", response_model=schemas.LedgerEntryOut)
def create_expense(
    payload: schemas.ExpenseCreate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_superadmin),
):
    if payload.amount <= 0:
        raise HTTPException(400, "مبلغ هزینه باید بزرگ‌تر از صفر باشد")
    entry = accounting.record(
        db, "expense", payload.amount,
        actor_admin_id=current.id,
        category=(payload.category or "").strip() or None,
        note=(payload.note or "").strip() or None,
        created_at=payload.created_at,
    )
    db.commit()
    db.refresh(entry)
    return schemas.LedgerEntryOut.model_validate(entry)


@router.delete("/expenses/{entry_id}")
def delete_expense(
    entry_id: int,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_superadmin), _confirm=Depends(require_confirm_password)):
    """Cancels a manual expense by posting a REVERSING entry, rather than
    erasing the original.

    Only manual expense rows can be cancelled at all - automatic
    sale/credit rows are the books themselves and stay untouchable.

    Changed 2026-09: this used to db.delete() the row. That silently
    rewrote the profit of a period that had already been reported - the
    expense simply vanished, with nothing left to say it had ever been
    entered or who removed it. A reversing entry is what bookkeeping does
    instead: both rows stay, they cancel out to zero, and the history of
    the correction is itself part of the record.
    """
    entry = db.get(models.LedgerEntry, entry_id)
    if not entry or entry.kind != "expense":
        raise HTTPException(404, "هزینه پیدا نشد")
    if (entry.amount or 0) < 0:
        raise HTTPException(400, "این ردیف خودش یک ردیف اصلاحی است")
    already = (
        db.query(models.LedgerEntry)
        .filter(models.LedgerEntry.kind == "expense", models.LedgerEntry.note.like(f"%#{entry.id})"))
        .first()
    )
    if already is not None:
        raise HTTPException(400, "این هزینه قبلاً برگشت خورده است")
    accounting.record(
        db, "expense", -abs(entry.amount or 0),
        actor_admin_id=current.id,
        category=entry.category,
        note=f"برگشت هزینه (#{entry.id})",
    )
    db.commit()
    return {"ok": True, "reversed": True}


@router.get("/receivables")
def list_receivables(
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    """Per-reseller current account: what they were charged (credit granted
    + metered usage), what they have paid, what is still owed. This is the
    list the "ثبت دریافت" action below is used from."""
    as_of = _parse_date(date_to, end=True)
    start = accounting.receivables_start(db)
    return {
        "items": accounting.receivables_by_admin(db, current, date_to=as_of),
        "total": accounting.receivables_total(db, current, date_to=as_of),
        "start_at": start,
    }


@router.post("/receivables/reset")
def reset_receivables(
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_superadmin),
):
    """Draws a line under the reseller current-account: everything owed up
    to right now stops counting, and collection starts fresh from here.

    Nothing is deleted. The credit grants, usage charges and payments all
    stay exactly where they are in the ledger - this only moves the point
    the balance is measured from (see
    models.PanelSettings.receivables_start_at), which is why it is safe and
    why the old figures remain auditable in the transactions list.

    Needed because the feature was introduced on top of years of existing
    top-up history: read as debt, that history claimed every credit ever
    granted was still outstanding.
    """
    settings = db.query(models.PanelSettings).first()
    if settings is None:
        raise HTTPException(400, "تنظیمات پنل پیدا نشد")
    settings.receivables_start_at = dt.datetime.utcnow()
    db.commit()
    return {"ok": True, "start_at": settings.receivables_start_at}


@router.post("/payments", response_model=schemas.LedgerEntryOut)
def record_payment(
    payload: schemas.AdminPaymentCreate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    """Records money actually COLLECTED from a reseller.

    Granting credit is not income - it is routinely handed over before it
    is paid for. This is the other half: the point at which the money
    genuinely arrives, which is what the superadmin's net profit counts
    (see services/accounting.py's summary).

    Scoped like everything else here: you can only record a payment from
    an account you can already see.
    """
    if payload.amount <= 0:
        raise HTTPException(400, "مبلغ دریافتی باید بزرگ‌تر از صفر باشد")
    target = db.get(models.AdminUser, payload.admin_id)
    if target is None:
        raise HTTPException(404, "نماینده پیدا نشد")
    allowed = accounting.visible_admin_ids(db, current)
    if allowed is not None and target.id not in allowed:
        raise HTTPException(404, "نماینده پیدا نشد")
    if target.id == current.id:
        raise HTTPException(400, "نمی‌توانید از خودتان دریافت ثبت کنید")

    entry = accounting.record(
        db, accounting.PAYMENT_KIND, payload.amount,
        admin_id=target.id,
        actor_admin_id=current.id,
        payment_method=(payload.payment_method or "").strip() or None,
        note=(payload.note or "").strip() or None,
        created_at=payload.created_at,
    )
    db.commit()
    db.refresh(entry)
    return schemas.LedgerEntryOut.model_validate(entry)


@router.get("/export")
def export_xlsx(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    kind: Optional[str] = None,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(get_current_admin),
):
    """Excel export of the (role-scoped, filtered) transactions - same
    openpyxl+FileResponse approach as routers/users.py's export."""
    from openpyxl import Workbook

    q = accounting.apply_filters(
        accounting.scoped_query(db, current),
        date_from=_parse_date(date_from), date_to=_parse_date(date_to, end=True),
        kind=kind,
    ).order_by(models.LedgerEntry.created_at.desc())

    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    headers = [
        "ID", "Kind", "Amount (Toman)", "Customer", "Admin/Seller",
        "Package", "Payment method", "Card ID", "Discount code",
        "Discount amount", "Category", "Note", "Created at",
    ]
    for col, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col, value=h)
    for row, e in enumerate(q.all(), start=2):
        ws.cell(row=row, column=1, value=e.id)
        ws.cell(row=row, column=2, value=e.kind)
        ws.cell(row=row, column=3, value=e.amount)
        ws.cell(row=row, column=4, value=e.username_snapshot)
        ws.cell(row=row, column=5, value=e.admin_username_snapshot)
        ws.cell(row=row, column=6, value=e.package_name_snapshot)
        ws.cell(row=row, column=7, value=e.payment_method)
        ws.cell(row=row, column=8, value=e.payment_card_id)
        ws.cell(row=row, column=9, value=e.discount_code)
        ws.cell(row=row, column=10, value=e.discount_amount)
        ws.cell(row=row, column=11, value=e.category)
        ws.cell(row=row, column=12, value=e.note)
        ws.cell(row=row, column=13, value=fmt_jalali(e.created_at) if e.created_at else None)

    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    wb.save(path)
    filename = f"accounting-{dt.datetime.utcnow().strftime('%Y%m%d-%H%M')}.xlsx"
    return FileResponse(path, filename=filename, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
