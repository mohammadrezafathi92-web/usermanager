import datetime as dt

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from .database import get_db
from .security import decode_access_token
from .permissions import effective_permissions
from .services import hierarchy
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
    their stored `permissions` value. Level-2 Admins (see services/
    hierarchy.py) ALSO always pass, full stop: the 3-tier hierarchy feature
    gives them full menu access within their own tree by design (that's the
    whole point of "هر ادمین یه پنل کامل داره") - only level-3 Sellers are
    ever actually gated by the granular permissions.PERMISSION_CHOICES
    checkboxes, same as how every non-superadmin admin worked before this
    feature existed."""

    def _checker(admin: models.AdminUser = Depends(get_current_admin)) -> models.AdminUser:
        if admin.is_superadmin or hierarchy.role(admin) == hierarchy.ROLE_ADMIN or perm in effective_permissions(admin):
            return admin
        raise HTTPException(status.HTTP_403_FORBIDDEN, "شما به این بخش دسترسی ندارید")

    return _checker


def require_admin_or_above(admin: models.AdminUser = Depends(get_current_admin)) -> models.AdminUser:
    """Gate for the (now hierarchy-aware) /api/admins router: superadmins
    manage level-2 Admins, and level-2 Admins manage their OWN level-3
    Sellers - both roles reach this router, each endpoint inside further
    scopes what they're actually allowed to see/touch (see routers/
    admins.py). Sellers can never reach this router at all - they're never
    allowed to create/manage anyone (services/hierarchy.py's
    can_create_sub_admin)."""
    if admin.is_superadmin or hierarchy.role(admin) == hierarchy.ROLE_ADMIN:
        return admin
    raise HTTPException(status.HTTP_403_FORBIDDEN, "این بخش برای شما در دسترس نیست")


def has_own_bot(admin: models.AdminUser) -> bool:
    """Whether this account runs its own dedicated Telegram bot (see
    models.AdminUser.own_bot_token / runner.start_admin_bot)."""
    return bool(getattr(admin, "own_bot_token", None)) and bool(getattr(admin, "own_bot_enabled", False))


def require_ads_access(admin: models.AdminUser = Depends(get_current_admin)) -> models.AdminUser:
    """Gate for the «تبلیغات» router.

    Admin-tier as before, PLUS a level-3 Seller who has their own dedicated
    bot. The section used to be admin-or-above on the reasoning that "a
    level-3 Seller has neither a channel nor a bot of their own" - the
    second half of that stopped being true once Sellers could be given their
    own bot, and services/ads.py already sends through whichever bot the
    channel's owner runs (_bot_for). So a Seller with a bot has everything
    the feature needs and was being kept out by a stale assumption
    (2026-09: "قسمت تبلیغات رو هم باید برای اونا که بات اختصاصی دارن بشه
    فعال کرد").

    A Seller WITHOUT a bot is still refused, and told why - the shared panel
    bot is not theirs to advertise through.
    """
    if admin.is_superadmin or hierarchy.role(admin) == hierarchy.ROLE_ADMIN:
        return admin
    # Two conditions for a Seller, and they answer different questions:
    # manage_ads is whether the superadmin GRANTED this, having their own bot
    # is whether it can physically work.
    if not has_own_bot(admin):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "برای استفاده از بخش تبلیغات باید ربات اختصاصی خودتان را فعال کرده باشید",
        )
    from .permissions import effective_permissions

    if "manage_ads" not in effective_permissions(admin):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "این بخش برای شما در دسترس نیست")
    return admin


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


def require_confirm_password(
    x_confirm_password: str = Header(None, alias="X-Confirm-Password"),
    admin: models.AdminUser = Depends(get_current_admin),
) -> models.AdminUser:
    """Re-asks for the logged-in admin's OWN password before a destructive
    action goes through (panel owner's request, 2026-08-10). Deletions here
    are irreversible and often wide-reaching - a whole customer, a service
    and its live connections, a node every customer depends on - so a
    stolen/borrowed open session, or a mis-click on a confirm dialog,
    shouldn't be enough on its own.

    The password travels in the X-Confirm-Password header rather than the
    body so it works uniformly for DELETE requests (which don't reliably
    carry a body) and never ends up in a URL/query string that could be
    logged. It's verified against the same hash used at login and is never
    stored anywhere.
    """
    from .security import verify_password  # local import - keeps deps import-light

    if not x_confirm_password or not verify_password(x_confirm_password, admin.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="برای انجام این عملیات باید رمز عبور خودتان را وارد کنید",
        )
    return admin
