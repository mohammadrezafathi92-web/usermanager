"""Admin CRUD for promo/discount codes (کد تخفیف) - see models.DiscountCode
for the field meanings and models.DiscountCodeRedemption for the per-user
usage audit trail. Separate feature from the referral program (User.
referral_code) - see routers/bot.py for the bot-facing validate/redeem
endpoints the Telegram bot calls at checkout.

Codes are panel-wide (like PanelSettings), not scoped per owner_admin_id -
gated behind require_admin_or_above (superadmin or level-2 Admin only),
same as the rest of the payment/checkout configuration. A level-3 Seller
is structurally blocked from this whole router (not just a checkbox that
happens to be unset) because a code isn't scoped to one Admin's tree -
a Seller creating/editing/deleting one would affect every other Admin's
and Seller's checkout too. See permissions.py's module docstring for the
full reasoning, confirmed explicitly with the panel owner."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_admin_or_above

router = APIRouter(
    prefix="/api/discount-codes",
    tags=["discount-codes"],
    dependencies=[Depends(require_admin_or_above)],
)


def _get_or_404(db: Session, code_id: int) -> models.DiscountCode:
    row = db.get(models.DiscountCode, code_id)
    if not row:
        raise HTTPException(404, "کد تخفیف پیدا نشد")
    return row


@router.get("", response_model=list[schemas.DiscountCodeOut])
def list_discount_codes(db: Session = Depends(get_db)):
    return db.query(models.DiscountCode).order_by(models.DiscountCode.id.desc()).all()


@router.post("", response_model=schemas.DiscountCodeOut)
def create_discount_code(payload: schemas.DiscountCodeCreate, db: Session = Depends(get_db)):
    code = payload.code.strip().upper()
    if not code:
        raise HTTPException(400, "کد تخفیف نمی‌تواند خالی باشد")
    if db.query(models.DiscountCode).filter(models.DiscountCode.code == code).first():
        raise HTTPException(400, "این کد تخفیف قبلاً ثبت شده است")
    row = models.DiscountCode(**{**payload.model_dump(), "code": code})
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.put("/{code_id}", response_model=schemas.DiscountCodeOut)
def update_discount_code(code_id: int, payload: schemas.DiscountCodeUpdate, db: Session = Depends(get_db)):
    row = _get_or_404(db, code_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.delete("/{code_id}")
def delete_discount_code(code_id: int, db: Session = Depends(get_db)):
    row = _get_or_404(db, code_id)
    db.delete(row)
    db.commit()
    return {"ok": True}


@router.get("/{code_id}/redemptions", response_model=list[schemas.DiscountCodeRedemptionOut])
def list_redemptions(code_id: int, db: Session = Depends(get_db)):
    _get_or_404(db, code_id)
    return (
        db.query(models.DiscountCodeRedemption)
        .filter(models.DiscountCodeRedemption.code_id == code_id)
        .order_by(models.DiscountCodeRedemption.id.desc())
        .all()
    )
