from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_superadmin
from ..services.keys import generate_api_key

# Superadmin-only (like routers/backup.py's full-DB backup and
# routers/telegram_bot_settings.py's global bot settings - see their
# docstrings): models.ApiKey has NO owner_admin_id/scope at all, it's one
# flat panel-wide credential list, and a key used against /api/bot/*
# decides its OWN data scope via a caller-supplied owner_admin_id (see
# deps.py's get_bot_api_key + routers/bot.py) rather than being bound to
# whoever created it. The old `require_permission("manage_api_keys")` let
# a level-2 Admin (who bypasses all permission checks) - and any Seller
# explicitly granted the checkbox - see/toggle/delete every key in the
# system, including ones that don't belong to them at all. Confirmed with
# the panel owner that these keys are only ever created/used by the
# superadmin themself, so this is a straight lockdown, not a missing
# feature - a scoped-per-admin equivalent (like own_bot_token) would be a
# separate, deliberately-designed feature if ever needed.
router = APIRouter(prefix="/api/api-keys", tags=["api-keys"], dependencies=[Depends(require_superadmin)])


@router.get("", response_model=list[schemas.ApiKeyOut])
def list_keys(db: Session = Depends(get_db)):
    return db.query(models.ApiKey).order_by(models.ApiKey.id.desc()).all()


@router.post("", response_model=schemas.ApiKeyOut)
def create_key(payload: schemas.ApiKeyCreate, db: Session = Depends(get_db)):
    key = models.ApiKey(label=payload.label, key=generate_api_key())
    db.add(key)
    db.commit()
    db.refresh(key)
    return key


@router.post("/{key_id}/toggle", response_model=schemas.ApiKeyOut)
def toggle_key(key_id: int, db: Session = Depends(get_db)):
    key = db.get(models.ApiKey, key_id)
    if not key:
        raise HTTPException(404, "کلید پیدا نشد")
    key.enabled = not key.enabled
    db.commit()
    db.refresh(key)
    return key


@router.delete("/{key_id}")
def delete_key(key_id: int, db: Session = Depends(get_db)):
    key = db.get(models.ApiKey, key_id)
    if not key:
        raise HTTPException(404, "کلید پیدا نشد")
    db.delete(key)
    db.commit()
    return {"ok": True}
