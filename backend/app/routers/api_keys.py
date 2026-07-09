from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_permission
from ..services.keys import generate_api_key

router = APIRouter(prefix="/api/api-keys", tags=["api-keys"], dependencies=[Depends(require_permission("manage_settings"))])


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
