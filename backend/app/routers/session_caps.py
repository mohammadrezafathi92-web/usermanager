"""Superadmin control of the per-Purchase session-cap rollout
(services/session_cap_mode.py, services/purchase_cap_repair.py).

Every route is superadmin-only; every change (mode, Purchase cap repair)
also re-asks the admin's own password (X-Confirm-Password) and records an
audit row. Reads never change anything.
"""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import purchase_cap_repair, session_cap_mode

router = APIRouter(prefix="/api/session-caps", tags=["session-caps"], dependencies=[Depends(require_superadmin)])


def get_session_factory():
    from ..database import SessionLocal
    return SessionLocal


class ModeRequest(BaseModel):
    target: Literal["legacy", "shadow", "enforced"]
    reason: str


class RepairPreviewRequest(BaseModel):
    purchase_id: int
    new_value: int


class RepairExecuteRequest(BaseModel):
    purchase_id: int
    expected_user_id: int
    expected_current: Optional[int] = None
    new_value: int
    preview_token: str
    reason: str


@router.get("/status")
def status(db: Session = Depends(get_db)):
    return session_cap_mode.status(db)


@router.post("/mode")
def change_mode(payload: ModeRequest, admin=Depends(require_confirm_password),
                factory=Depends(get_session_factory)):
    return session_cap_mode.change_mode(factory, admin.id, payload.target, payload.reason)


@router.get("/shadow-events")
def shadow_events(db: Session = Depends(get_db), kind: Optional[str] = None,
                  limit: int = Query(200, ge=1, le=2000)):
    query = db.query(models.SessionCapShadowEvent)
    if kind:
        query = query.filter(models.SessionCapShadowEvent.kind == kind)
    rows = query.order_by(models.SessionCapShadowEvent.id.desc()).limit(limit).all()
    return [dict(kind=r.kind, user_id=r.user_id, purchase_id=r.purchase_id, connection_id=r.connection_id,
                 bucket_start=r.bucket_start, occurrences=r.occurrences, legacy_limit=r.legacy_limit,
                 legacy_count=r.legacy_count, new_limit=r.new_limit, new_count=r.new_count,
                 first_at=r.first_at, last_at=r.last_at) for r in rows]


@router.get("/shadow-summary")
def shadow_summary(db: Session = Depends(get_db)):
    """Per kind and Purchase: how many 5-minute buckets disagreed."""
    E = models.SessionCapShadowEvent
    rows = (db.query(E.kind, E.purchase_id, E.user_id, func.count(), func.sum(E.occurrences), func.max(E.last_at))
            .group_by(E.kind, E.purchase_id, E.user_id).all())
    return [dict(kind=k, purchase_id=p, user_id=u, buckets=b, occurrences=int(o or 0), last_at=at)
            for k, p, u, b, o, at in sorted(rows, key=lambda r: (r[0], -r[3]))]


@router.post("/purchase-repairs/preview")
def repair_preview(payload: RepairPreviewRequest, db: Session = Depends(get_db)):
    return purchase_cap_repair.preview(db, payload.purchase_id, payload.new_value)


@router.post("/purchase-repairs/execute")
def repair_execute(payload: RepairExecuteRequest, admin=Depends(require_confirm_password),
                   factory=Depends(get_session_factory)):
    return purchase_cap_repair.execute(
        factory, admin.id, purchase_id=payload.purchase_id, expected_user_id=payload.expected_user_id,
        expected_current=payload.expected_current, new_value=payload.new_value,
        preview_token=payload.preview_token, reason=payload.reason)


@router.get("/purchase-repairs")
def repair_audit(db: Session = Depends(get_db), limit: int = Query(200, ge=1, le=2000)):
    rows = db.query(models.PurchaseCapRepair).order_by(models.PurchaseCapRepair.id.desc()).limit(limit).all()
    return [dict(id=r.id, purchase_id=r.purchase_id, user_id=r.user_id, before=r.before_value, after=r.after_value,
                 user_cap_at_repair=r.user_cap_at_repair, scope_connection_ids=r.scope_connection_ids,
                 reason=r.reason, admin_id=r.admin_id, created_at=r.created_at) for r in rows]
