"""Admin CRUD for promo/discount codes (کد تخفیف) - see models.DiscountCode
for the field meanings and models.DiscountCodeRedemption for the per-user
usage audit trail. Separate feature from the referral program (User.
referral_code) - see routers/bot.py for the bot-facing validate/redeem
endpoints the Telegram bot calls at checkout.

Each tier - including a level-3 Seller now - manages their OWN codes,
used only in THEIR OWN bot (models.DiscountCode.owner_admin_id, NULL for a
superadmin's own). A level-2 Admin additionally gets a READ-ONLY roll-up
view of their own Sellers' codes here (oversight, like the customer
roster) - they can see but never edit/delete a Seller's code. A superadmin
does NOT roll up to see Admins'/Sellers' codes at all - fully isolated,
same as Package/Tutorial. Confirmed with the panel owner 2026-07-19."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin
from ..services import hierarchy

router = APIRouter(
    prefix="/api/discount-codes",
    tags=["discount-codes"],
    dependencies=[Depends(get_current_admin)],
)


def _own_scope(admin: models.AdminUser) -> Optional[int]:
    """The owner_admin_id value THIS account's own codes are stored
    under - None for a superadmin, their own id otherwise (same for an
    Admin or a Seller - both can own codes directly now)."""
    return None if admin.is_superadmin else admin.id


def _get_viewable_code(db: Session, code_id: int, admin: models.AdminUser) -> models.DiscountCode:
    """404s (not 403) for a code outside this account's VIEW scope (see
    hierarchy.accessible_discount_code_owner_ids - includes an Admin's own
    Sellers' codes, read-only). Used for reads (get/list-redemptions)."""
    row = db.get(models.DiscountCode, code_id)
    if not row:
        raise HTTPException(404, "کد تخفیف پیدا نشد")
    allowed = hierarchy.accessible_discount_code_owner_ids(db, admin)
    if row.owner_admin_id not in allowed:
        raise HTTPException(404, "کد تخفیف پیدا نشد")
    return row


def _get_owned_code(db: Session, code_id: int, admin: models.AdminUser) -> models.DiscountCode:
    """404s for anything except a code THIS account directly owns - even a
    level-2 Admin who can VIEW their Seller's code via roll-up can never
    edit/delete it here, only the Seller themself can (see this module's
    docstring). Used for writes (update/delete)."""
    row = db.get(models.DiscountCode, code_id)
    if not row:
        raise HTTPException(404, "کد تخفیف پیدا نشد")
    if row.owner_admin_id != _own_scope(admin):
        raise HTTPException(404, "کد تخفیف پیدا نشد")
    return row


def _out(row: models.DiscountCode) -> models.DiscountCode:
    """Bolts the owner's username on for display - lets a level-2 Admin's
    roll-up view tell their own codes apart from their Sellers' at a
    glance (same trick as routers/packages.py's _out())."""
    row.owner_admin_username = row.owner_admin.username if row.owner_admin else None
    return row


@router.get("", response_model=list[schemas.DiscountCodeOut])
def list_discount_codes(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    allowed = hierarchy.accessible_discount_code_owner_ids(db, admin)
    rows = (
        db.query(models.DiscountCode)
        .filter(hierarchy.owner_id_in_clause(models.DiscountCode.owner_admin_id, allowed))
        .order_by(models.DiscountCode.id.desc())
        .all()
    )
    return [_out(r) for r in rows]


@router.post("", response_model=schemas.DiscountCodeOut)
def create_discount_code(payload: schemas.DiscountCodeCreate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(400, "کد تخفیف نمی‌تواند خالی باشد")
    # `code` stays globally unique on purpose (see models.DiscountCode's
    # docstring) - not scoped per-owner.
    if db.query(models.DiscountCode).filter(models.DiscountCode.code == code).first():
        raise HTTPException(400, "این کد تخفیف قبلاً ثبت شده است")
    row = models.DiscountCode(**{**payload.model_dump(), "code": code, "owner_admin_id": _own_scope(admin)})
    db.add(row)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.put("/{code_id}", response_model=schemas.DiscountCodeOut)
def update_discount_code(code_id: int, payload: schemas.DiscountCodeUpdate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    row = _get_owned_code(db, code_id, admin)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return _out(row)


@router.delete("/{code_id}")
def delete_discount_code(code_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    row = _get_owned_code(db, code_id, admin)
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/{code_id}/redemptions", response_model=list[schemas.DiscountCodeRedemptionOut])
def list_redemptions(code_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    _get_viewable_code(db, code_id, admin)
    return (
        db.query(models.DiscountCodeRedemption)
        .filter(models.DiscountCodeRedemption.code_id == code_id)
        .order_by(models.DiscountCodeRedemption.id.desc())
        .all()
    )
