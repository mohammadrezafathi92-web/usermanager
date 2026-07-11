from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import verify_password, create_access_token, hash_password
from ..deps import get_current_admin
from ..permissions import effective_permissions


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=schemas.Token)
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    admin = db.query(models.AdminUser).filter(models.AdminUser.username == form_data.username).first()
    if not admin or not verify_password(form_data.password, admin.hashed_password):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="نام کاربری یا رمز عبور اشتباه است")
    token = create_access_token(admin.username)
    return schemas.Token(access_token=token)


@router.get("/me")
def me(admin: models.AdminUser = Depends(get_current_admin)):
    return {
        "username": admin.username,
        "is_superadmin": admin.is_superadmin,
        "permissions": sorted(effective_permissions(admin)),
    }


@router.post("/change-password")
def change_password(
    payload: ChangePasswordRequest,
    admin: models.AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    if not verify_password(payload.old_password, admin.hashed_password):
        raise HTTPException(status_code=400, detail="رمز عبور فعلی اشتباه است")
    admin.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"ok": True}
