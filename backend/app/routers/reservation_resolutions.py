"""Case-by-case resolution of User-level reservations before the joint
activation (services/reservation_resolution.py). Superadmin only; recording
a decision and executing it also re-ask the admin's own password, and an
execution needs X-Idempotency-Key."""
from typing import Optional

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import ip_guard, reservation_resolution as resolution

router = APIRouter(prefix="/api/accounting/reservation-resolutions", tags=["reservation-resolutions"],
                   dependencies=[Depends(require_superadmin)])


class EvidenceRefs(BaseModel):
    ledger_entry_ids: list[int] = []
    notes: Optional[str] = None


class DecisionIn(BaseModel):
    user_id: int
    action: str
    reason: str
    target_purchase_id: Optional[int] = None
    cap_policy: Optional[str] = None
    delivered_by_purchase_ids: list[int] = []
    evidence_refs: EvidenceRefs = EvidenceRefs()


class PreviewIn(BaseModel):
    decision_id: int


class ExecuteIn(BaseModel):
    decision_id: int
    preview_token: str
    typed_username: str


@router.get("/candidates")
def candidates(db: Session = Depends(get_db)):
    return resolution.candidates(db)


@router.post("/decisions")
def decide(payload: DecisionIn, db: Session = Depends(get_db),
           admin: models.AdminUser = Depends(require_confirm_password)):
    return resolution.decide(db, admin, user_id=payload.user_id, action=payload.action, reason=payload.reason,
                             target_purchase_id=payload.target_purchase_id, cap_policy=payload.cap_policy,
                             delivered_by_purchase_ids=payload.delivered_by_purchase_ids,
                             evidence_refs=payload.evidence_refs.model_dump())


@router.post("/preview")
def preview(payload: PreviewIn, db: Session = Depends(get_db)):
    return resolution.preview(db, payload.decision_id)


@router.post("/execute")
def execute(payload: ExecuteIn, request: Request, db: Session = Depends(get_db),
            admin: models.AdminUser = Depends(require_confirm_password),
            x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key")):
    return resolution.execute(db, admin, decision_id=payload.decision_id, preview_token=payload.preview_token,
                              typed_username=payload.typed_username, idempotency_key=x_idempotency_key or "",
                              ip=ip_guard.resolve_client_ip(request))
