"""Panel-wide settings that aren't tied to a specific node - currently just
the card-to-card payment info the sales bot shows customers at checkout."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_permission

router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_permission("manage_settings"))])


def _get_or_create(db: Session) -> models.PanelSettings:
    row = db.get(models.PanelSettings, 1)
    if not row:
        row = models.PanelSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@router.get("", response_model=schemas.PanelSettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return _get_or_create(db)


@router.put("", response_model=schemas.PanelSettingsOut)
def update_settings(payload: schemas.PanelSettingsUpdate, db: Session = Depends(get_db)):
    row = _get_or_create(db)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row
