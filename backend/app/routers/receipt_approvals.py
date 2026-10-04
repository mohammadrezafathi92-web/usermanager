"""Operator endpoints for the receipt-approval runtime mode (Receipt Void
design 5.5, section 18). Superadmin only; a mode change also asks for the
admin's own password."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import SessionLocal, engine, get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import receipt_approval_runtime as runtime
from ..services import receipt_void_schema

router = APIRouter(
    prefix="/api/accounting/receipt-approvals", tags=["receipt-approvals"],
    dependencies=[Depends(require_superadmin)],
)


class RuntimeModeUpdate(BaseModel):
    registration_mode: Optional[str] = None
    auto_rate_limit_mode: Optional[str] = None


@router.get("/runtime-mode")
def get_runtime_mode(db: Session = Depends(get_db)):
    return runtime.describe(db)


@router.put("/runtime-mode")
def put_runtime_mode(payload: RuntimeModeUpdate, db: Session = Depends(get_db),
                     _confirm=Depends(require_confirm_password)):
    if not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    try:
        runtime.set_requested_mode(db, registration_mode=payload.registration_mode,
                                   auto_rate_limit_mode=payload.auto_rate_limit_mode)
        db.commit()
    except runtime.ModeChangeRejected as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code)
    return runtime.describe(db)


@router.post("/runtime-mode/recheck")
def recheck_runtime_mode(db: Session = Depends(get_db)):
    if not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    receipt_void_schema.compute_key_identity(engine, SessionLocal)
    return runtime.describe(db)
