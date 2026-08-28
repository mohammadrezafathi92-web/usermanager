"""On-demand database health report - superadmin-only, read-only. See
services/db_health.py's module docstring for what it checks and why.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_superadmin
from ..services import db_health

router = APIRouter(prefix="/api/db-health", tags=["db-health"], dependencies=[Depends(require_superadmin)])


@router.get("/check")
def check(db: Session = Depends(get_db)):
    return db_health.run_health_check(db)
