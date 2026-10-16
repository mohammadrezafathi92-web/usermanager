"""Superadmin control of the wallet generation (Receipt Void P9, 20.2 - 20.3,
21): the read-only preflight, the one-transaction cutover to the lot
generation, and its narrow rollback. Every change asks for the password."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import receipt_void_schema, wallet_cutover

router = APIRouter(prefix="/api/wallet", tags=["wallet"], dependencies=[Depends(require_superadmin)])


def _ready():
    if not receipt_void_schema.is_ready():
        raise HTTPException(503, "receipt_void_schema_not_ready")


@router.get("/status")
def status(db: Session = Depends(get_db)):
    _ready()
    return wallet_cutover.status(db)


@router.get("/preflight")
def preflight(db: Session = Depends(get_db)):
    _ready()
    return wallet_cutover.preflight(db)


@router.post("/cutover")
def cutover(db: Session = Depends(get_db), _confirm=Depends(require_confirm_password)):
    _ready()
    return wallet_cutover.cutover(db)


@router.post("/rollback")
def rollback(db: Session = Depends(get_db), _confirm=Depends(require_confirm_password)):
    _ready()
    return wallet_cutover.rollback(db)


@router.get("/reconcile")
def reconcile(db: Session = Depends(get_db)):
    """Read-only checks (Receipt Void 20.5): alerts, never a repair."""
    from ..services import wallet_reconcile
    _ready()
    return wallet_reconcile.check(db)
