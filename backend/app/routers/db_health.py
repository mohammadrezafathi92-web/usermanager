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


@router.post("/optimize")
def optimize(db: Session = Depends(get_db)):
    """"بهینه‌سازی دیتابیس" button - deletes stale/expired log rows (see
    services/db_health.py's optimize_database docstring for the exact,
    deliberately narrow scope) and VACUUMs/ANALYZEs the database. Unlike
    /check, this one writes - still superadmin-only via the router-level
    dependency above."""
    return db_health.optimize_database(db)
