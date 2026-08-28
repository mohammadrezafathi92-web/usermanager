"""Superadmin-facing view/management of services/ip_guard.py's block list.

Deliberately superadmin-only and small: ip_guard bans/unbans automatically
on its own; this router exists so an IP that got banned by mistake (a
shared office/NAT address, a misbehaving but legitimate integration) is not
stuck forever with no way back in except a direct database edit, and so a
known-bad IP can be blocked by hand before it ever trips the automatic
counters.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_superadmin
from ..services import ip_guard

router = APIRouter(prefix="/api/ip-bans", tags=["ip-bans"], dependencies=[Depends(require_superadmin)])


class ManualBanIn(BaseModel):
    ip: str
    reason: str | None = None


@router.get("")
def list_bans(db: Session = Depends(get_db)):
    return [
        {
            "ip": b.ip,
            "reason": b.reason,
            "hit_count": b.hit_count,
            "is_manual": b.is_manual,
            "banned_at": b.banned_at.isoformat() if b.banned_at else None,
        }
        for b in ip_guard.list_bans(db)
    ]


@router.post("")
def add_ban(payload: ManualBanIn, db: Session = Depends(get_db)):
    ip = (payload.ip or "").strip()
    if not ip:
        raise HTTPException(400, "آی‌پی خالی است")
    ip_guard.ban(db, ip, reason=(payload.reason or "افزوده‌شده دستی توسط ادمین").strip(), manual=True)
    return {"ok": True}


@router.delete("/{ip}")
def remove_ban(ip: str, db: Session = Depends(get_db)):
    if not ip_guard.unban(db, ip):
        raise HTTPException(404, "این آی‌پی در لیست مسدودی نیست")
    return {"ok": True}
