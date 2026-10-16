"""Receipt void (Receipt Void 17, 18; P10 preview, P11 execute): superadmin
only; execute and force also re-ask the admin's own password
(X-Confirm-Password). Every endpoint is behind its own phase flag and 404s
while it is off (receipt_void_flags); the flags themselves change only with
the password. The approval's owner snapshot is checked on every lookup -
missing and not allowed are both 404."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import ip_guard, provisioning_requests, receipt_void_engine as engine
from ..services import receipt_void_flags as flags, receipt_void_operator as operator, receipt_void_scope as scope

router = APIRouter(prefix="/api/accounting/receipt-void", tags=["receipt-void"],
                   dependencies=[Depends(require_superadmin)])


class PreviewIn(BaseModel):
    approval_uuid: Optional[str] = None
    ledger_entry_id: Optional[int] = None


class ExecuteIn(BaseModel):
    preview_token: str
    typed_username: str
    reason: str
    danger_ack: bool = False


class RetryIn(BaseModel):
    reason: Optional[str] = None


class ForceIn(BaseModel):
    step_key: str
    reason: str
    danger_ack: bool = False


class FlagIn(BaseModel):
    flag: Literal["preview", "execute", "worker"]
    enabled: bool


@router.get("/flags")
def get_flags(db: Session = Depends(get_db)):
    return flags.state(db)


@router.put("/flags")
def put_flag(payload: FlagIn, db: Session = Depends(get_db),
             _confirm: models.AdminUser = Depends(require_confirm_password)):
    return flags.set_flag(db, payload.flag, payload.enabled)


@router.post("/preview")
def preview(payload: PreviewIn, db: Session = Depends(get_db),
            current: models.AdminUser = Depends(require_superadmin)):
    flags.require(db, "preview")
    return scope.preview(db, current, approval_uuid=payload.approval_uuid, ledger_entry_id=payload.ledger_entry_id)


@router.post("/execute")
def execute(payload: ExecuteIn, request: Request, db: Session = Depends(get_db),
            current: models.AdminUser = Depends(require_confirm_password)):
    _superadmin(current)
    factory = provisioning_requests.session_factory_for(db)
    db.rollback()
    return engine.execute(factory, current, preview_token=payload.preview_token,
                          typed_username=payload.typed_username, reason=payload.reason,
                          danger_ack=payload.danger_ack, ip=ip_guard.resolve_client_ip(request))


@router.get("/status/{operation_id}")
def status(operation_id: int, db: Session = Depends(get_db),
           current: models.AdminUser = Depends(require_superadmin)):
    flags.require(db, "preview")
    return operator.status(db, current, operation_id)


@router.post("/retry/{operation_id}")
def retry(operation_id: int, payload: Optional[RetryIn] = None, db: Session = Depends(get_db),
          current: models.AdminUser = Depends(require_superadmin)):
    factory = provisioning_requests.session_factory_for(db)
    db.rollback()
    return operator.retry(factory, current, operation_id, reason=payload.reason if payload else None)


@router.post("/force-step/{operation_id}")
def force_step(operation_id: int, payload: ForceIn, db: Session = Depends(get_db),
               current: models.AdminUser = Depends(require_confirm_password)):
    _superadmin(current)
    factory = provisioning_requests.session_factory_for(db)
    db.rollback()
    return operator.force_step(factory, current, operation_id, step_key=payload.step_key, reason=payload.reason,
                               danger_ack=payload.danger_ack)


@router.get("/audit/{approval_uuid}")
def audit(approval_uuid: str, db: Session = Depends(get_db),
          current: models.AdminUser = Depends(require_superadmin)):
    flags.require(db, "preview")
    return operator.audit(db, current, approval_uuid)


def _superadmin(admin: models.AdminUser) -> None:
    # require_confirm_password resolves the admin itself; the router-level
    # dependency already refused a non-superadmin - this keeps it explicit.
    if not admin.is_superadmin:
        raise HTTPException(403, "superadmin_required")
