import datetime as dt

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .database import get_db
from .security import decode_access_token
from .permissions import parse_permissions
from . import models

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")


def get_current_admin(
    token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)
) -> models.AdminUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="اعتبار ورود نامعتبر است",
        headers={"WWW-Authenticate": "Bearer"},
    )
    username = decode_access_token(token)
    if username is None:
        raise credentials_exception
    admin = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()
    if admin is None:
        raise credentials_exception
    return admin


def require_superadmin(admin: models.AdminUser = Depends(get_current_admin)) -> models.AdminUser:
    """Gate for endpoints only the main admin may use (managing other
    admins) - a sub-admin can never grant themself or anyone else more
    access through the regular API, since this check can't be satisfied by
    any combination of `permissions`."""
    if not admin.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "این بخش فقط برای ادمین اصلی است")
    return admin


def require_permission(perm: str):
    """Returns a FastAPI dependency gating an endpoint behind one of
    permissions.PERMISSION_CHOICES - superadmins always pass regardless of
    their stored `permissions` value."""

    def _checker(admin: models.AdminUser = Depends(get_current_admin)) -> models.AdminUser:
        if admin.is_superadmin or perm in parse_permissions(admin.permissions):
            return admin
        raise HTTPException(status.HTTP_403_FORBIDDEN, "شما به این بخش دسترسی ندارید")

    return _checker


def get_bot_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> models.ApiKey:
    """Auth dependency for the external bot API. Callers must send a
    valid, enabled key in the `X-API-Key` header (created from the panel's
    Settings page)."""
    if not x_api_key:
        raise HTTPException(status_code=401, detail="هدر X-API-Key ارسال نشده است")
    key = (
        db.query(models.ApiKey)
        .filter(models.ApiKey.key == x_api_key, models.ApiKey.enabled == True)  # noqa: E712
        .first()
    )
    if not key:
        raise HTTPException(status_code=401, detail="کلید API نامعتبر یا غیرفعال است")
    # Throttled write: the bot calls this endpoint on every single
    # interaction, so committing last_used_at on every request adds needless
    # write/lock contention on the shared SQLite DB. Only bother persisting
    # it once every few minutes - plenty fresh for the "آخرین استفاده" field
    # in the panel's API keys UI.
    now = dt.datetime.utcnow()
    if not key.last_used_at or (now - key.last_used_at) > dt.timedelta(minutes=5):
        key.last_used_at = now
        db.commit()
    return key
