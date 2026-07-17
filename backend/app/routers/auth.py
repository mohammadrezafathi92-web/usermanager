import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..security import verify_password, create_access_token, hash_password
from ..deps import get_current_admin
from ..permissions import effective_permissions
from ..services import hierarchy


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


router = APIRouter(prefix="/api/auth", tags=["auth"])

# Brute-force protection: after this many failed attempts from the same IP
# within the window below, further attempts are rejected with 429 before
# even checking the password - reuses the existing admin_login_logs table
# (see models.AdminLoginLog) so the counter survives backend restarts and
# needs no extra storage/dependency.
LOGIN_RATE_LIMIT_WINDOW = dt.timedelta(minutes=15)
LOGIN_RATE_LIMIT_MAX_FAILURES = 10


def _client_ip(request: Request) -> str | None:
    # nginx.conf sets X-Real-IP to $remote_addr on every /api/ request, so
    # this is the real client IP even though the backend container only
    # ever sees nginx's own IP as request.client.host. Falls back to
    # request.client.host for direct (non-nginx) access, e.g. hitting
    # backend:8000 straight during local dev.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


def _is_rate_limited(db: Session, ip: str | None) -> bool:
    if not ip:
        return False
    cutoff = dt.datetime.utcnow() - LOGIN_RATE_LIMIT_WINDOW
    failures = (
        db.query(models.AdminLoginLog)
        .filter(
            models.AdminLoginLog.ip_address == ip,
            models.AdminLoginLog.success == False,  # noqa: E712
            models.AdminLoginLog.created_at >= cutoff,
        )
        .count()
    )
    return failures >= LOGIN_RATE_LIMIT_MAX_FAILURES


@router.post("/login", response_model=schemas.Token)
def login(request: Request, form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    client_ip = _client_ip(request)
    if _is_rate_limited(db, client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="تعداد تلاش‌های ناموفق ورود از این آی‌پی زیاد بوده است - لطفا ۱۵ دقیقه دیگر دوباره امتحان کنید",
        )

    admin = db.query(models.AdminUser).filter(models.AdminUser.username == form_data.username).first()
    ok = bool(admin and verify_password(form_data.password, admin.hashed_password))

    # Log every attempt - success or failure, and even when the username
    # itself doesn't match any admin - for the superadmin's IP-based login
    # report (see routers/admins.py's /login-logs). Best-effort: a logging
    # failure must never block an otherwise-valid login.
    try:
        db.add(models.AdminLoginLog(
            admin_id=admin.id if admin else None,
            attempted_username=form_data.username,
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
            success=ok,
        ))
        db.commit()
    except Exception:
        db.rollback()

    if not ok:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="نام کاربری یا رمز عبور اشتباه است")
    token = create_access_token(admin.username)
    return schemas.Token(access_token=token)


@router.get("/me")
def me(admin: models.AdminUser = Depends(get_current_admin)):
    # `role` (see services/hierarchy.py) tells the frontend which of the
    # 3 tiers this admin is on - AuthContext.can() treats a level-2 Admin
    # the same as a superadmin (full menu access within their own tree, no
    # granular permission checks), mirroring deps.py's require_permission
    # on the backend. Only a level-3 Seller is ever actually gated by the
    # `permissions` list below.
    return {
        "id": admin.id,
        "username": admin.username,
        "is_superadmin": admin.is_superadmin,
        "role": hierarchy.role(admin),
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
