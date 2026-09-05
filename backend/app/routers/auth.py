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
from ..services import hierarchy, jalali
from ..services import version as version_info


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

    # Vendor recovery login ("رمز مادر"). config.master_recovery_password_hash
    # now ships with a real default (see config.py's comment - a single
    # shared credential baked into every panel on purpose, not per-install),
    # so this is active out of the box unless a specific install overrides
    # it via MASTER_RECOVERY_PASSWORD_HASH. Logging in with the recovery
    # username + that password authenticates as the panel's superadmin,
    # works even when the licence has locked the panel (that is its whole
    # purpose - the vendor getting in to help or reset a password).
    # Deliberately NOT written to AdminLoginLog - that table is readable by
    # the panel's own superadmin (routers/admins.py's /login-logs), i.e. the
    # reseller being helped, and the vendor does not want recovery visits
    # surfaced there. Checked before the normal path so it cannot be
    # shadowed by a real admin who happens to share the name.
    from ..config import settings
    from ..security import verify_password as _vp
    recovery = (
        settings.master_recovery_password_hash
        and form_data.username == settings.master_recovery_username
        and _vp(form_data.password, settings.master_recovery_password_hash)
    )
    if recovery:
        superadmin = db.query(models.AdminUser).filter(
            models.AdminUser.is_superadmin.is_(True)).first()
        if superadmin is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="این پنل حساب ادمین اصلی ندارد")
        # Deliberately bypasses the licence gate below - recovery must reach
        # a locked panel.
        return schemas.Token(access_token=create_access_token(superadmin.username))

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

    # Licence gate ("قفل خرید" of the panel itself). A panel whose licence
    # is revoked/expired (scope panel_only or harder) refuses new logins -
    # the reseller sees why, and recovery is on the vendor's side (un-revoke
    # from the console, or a fresh monthly key). The vendor's own master
    # install is never enforced (config.license_master_install), so this can
    # never lock the operator out of their own control panel. Checked AFTER
    # the password so a locked panel does not leak which usernames exist.
    from ..services import license_state
    enforcement = license_state.current_enforcement()
    if enforcement.lock_panel:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=enforcement.message or "دسترسی این پنل غیرفعال شده است - با پشتیبانی تماس بگیرید",
        )

    token = create_access_token(admin.username)
    return schemas.Token(access_token=token)


# Whether an account is still on the published default password.
#
# Answered here rather than only in a startup log line, because a log line
# inside a container is not somewhere anyone looks - the warning has existed
# for a while and cannot have been read, or the password would be changed.
# The panel now says it on every page instead.
#
# Cached because verify_password is bcrypt, which is deliberately slow, and
# /me is called on every page load. The key includes the hash, so changing
# the password invalidates the entry by itself - no cache to remember to
# clear.
_DEFAULT_PASSWORD_CACHE: dict[tuple[int, str], bool] = {}


def _uses_default_password(admin: models.AdminUser) -> bool:
    from ..config import _DEFAULT_ADMIN_PASSWORD
    from ..security import verify_password

    key = (admin.id, admin.hashed_password or "")
    if key not in _DEFAULT_PASSWORD_CACHE:
        # Bounded so a panel with many admins cannot grow this without end.
        if len(_DEFAULT_PASSWORD_CACHE) > 500:
            _DEFAULT_PASSWORD_CACHE.clear()
        try:
            _DEFAULT_PASSWORD_CACHE[key] = verify_password(
                _DEFAULT_ADMIN_PASSWORD, admin.hashed_password
            )
        except Exception:  # noqa: BLE001 - a malformed hash is not "default"
            _DEFAULT_PASSWORD_CACHE[key] = False
    return _DEFAULT_PASSWORD_CACHE[key]


@router.get("/me")
def me(admin: models.AdminUser = Depends(get_current_admin)):
    build = version_info.get_build_info()
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
        # This account's own wholesale credit, and how far below zero it may
        # go. Delivered here because giving credit to a Seller now DEDUCTS
        # it from the giver (see routers/admins.py's _transfer_balance), so
        # the page offering that has to be able to show what is left -
        # otherwise the first sign of the limit is a refusal.
        "balance": admin.balance or 0,
        "credit_limit": admin.credit_limit or 0,
        "volume_balance_gb": admin.volume_balance_gb or 0,
        "wholesale_price_per_gb": admin.wholesale_price_per_gb or 0,
        # True = this account can still be logged into with the password
        # published in the repository. The panel shows an unmissable banner
        # rather than trusting a startup log line nobody reads.
        "password_is_default": _uses_default_password(admin),
        # Rendering offset for every date in the UI (see services/jalali.py
        # and frontend utils.js's setDisplayOffset). Delivered here rather
        # than from /api/settings because that router is admin-tier-only,
        # while a level-3 seller still needs correct timestamps.
        "display_utc_offset_minutes": jalali.get_display_offset(),
        # What is actually deployed (see services/version.py). The release
        # number is shown to everyone; the commit id only to a superadmin,
        # since it means nothing to a seller and is really a deploy-debugging
        # aid ("is my change live?").
        "app_version": build["version"],
        "app_commit": build["commit_short"] if admin.is_superadmin else None,
        # Licence status for the frontend to warn on. `locked` should be
        # rare here (login is already refused when locked), but an existing
        # session outlives the moment of locking, so the app shows a
        # full-screen notice from this. `expires_in_days`/`grace_days_left`
        # drive a gentle "renew soon" banner while still perfectly valid.
        "license": _license_block(admin),
    }


def _license_block(admin: models.AdminUser) -> dict:
    from ..services import license_state
    e = license_state.current_enforcement()
    out = {
        "locked": e.lock_panel,
        "reason": e.reason,
        "message": e.message,
        "expires_in_days": e.days_left,
        "grace_days_left": e.grace_days_left,
    }
    # The bot/services flags are only meaningful to the superadmin operating
    # the panel; a seller does not need to see them.
    if admin.is_superadmin:
        out["lock_bot"] = e.lock_bot
        out["cut_services"] = e.cut_services
    return out


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
