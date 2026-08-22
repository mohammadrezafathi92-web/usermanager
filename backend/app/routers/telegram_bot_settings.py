"""Settings for the built-in Telegram bot(s).

Two independent things live here now (3-tier hierarchy feature):
  1. The SINGLE shared/global bot (token, admin ids) - panel-wide
     infrastructure, superadmin only (see require_superadmin below) - a
     level-2 Admin managing their own tree has no business touching the
     one bot every OTHER Admin's customers might also be relying on.
  2. Each level-2 Admin's OR level-3 Seller's OWN dedicated bot
     (own_bot_token on their AdminUser row - see /my-bot endpoints at the
     bottom) - fully private to that account, runs concurrently with the
     shared bot and every other Admin/Seller's own bot (see
     telegram_bot/runner.py's multi-instance registry). A Seller's own bot
     scopes new customers to the Seller themself (not their parent Admin)
     and shows/charges the Seller's own resale price per package (see
     models.PackageSellerPrice, routers/bot.py's list_packages).
No .env file or SSH access needed for either - saving restarts the
relevant bot's in-process polling loop right away."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin, require_superadmin
from ..permissions import effective_permissions
from ..services import hierarchy
from ..telegram_bot import runner
from ..telegram_bot.config import parse_id_set

router = APIRouter(prefix="/api/telegram-bot", tags=["telegram-bot"], dependencies=[Depends(get_current_admin)])
_superadmin = Depends(require_superadmin)


def _require_admin_tier(admin: models.AdminUser) -> None:
    """Gate for the /my-bot endpoints below - a level-2 Admin OR a level-3
    Seller can have their own dedicated bot (a Seller's customers connect
    to THEIR bot and see THEIR own resale prices - see
    models.PackageSellerPrice/routers/bot.py's list_packages); a superadmin
    already has the shared/global bot above, so only they're excluded.

    A Seller additionally needs the own_bot permission. Running a sales bot
    on your own token is a real, separable capability - it is how a Seller
    reaches customers directly - so a level-2 Admin can decide whether a
    given sub-seller gets one. A level-2 Admin always may (see
    permissions.py's note on require_permission short-circuiting for them).
    """
    if hierarchy.role(admin) == hierarchy.ROLE_SUPERADMIN:
        raise HTTPException(403, "این بخش برای ادمین اصلی در دسترس نیست - از تنظیمات ربات مشترک استفاده کنید")
    if hierarchy.is_seller(admin) and "own_bot" not in effective_permissions(admin):
        raise HTTPException(403, "شما به این بخش دسترسی ندارید")


def _get_or_create(db: Session) -> models.BotSettings:
    row = db.get(models.BotSettings, 1)
    if not row:
        row = models.BotSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


# Schemes aiogram/aiohttp-socks can actually dial. Anything else is a typo
# that would only surface as "the bot silently stopped receiving messages",
# which is the hardest class of failure to diagnose in this project.
_PROXY_SCHEMES = ("socks5://", "socks4://", "http://", "https://")


def _validate_proxy_url(url: str) -> str:
    """Normalises and sanity-checks a transport proxy URL.

    Bare `host:port` is accepted and assumed to be socks5, because that is
    the form every proxy provider hands out and typing the scheme is the
    step people forget.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"socks5://{url}"
    # socks5h is what curl and most proxy providers call "SOCKS5 with remote
    # DNS", and people paste it verbatim - but aiohttp-socks rejects the
    # scheme outright with a ValueError deep inside bot construction. It is
    # rewritten rather than refused because the distinction does not exist
    # here anyway: python-socks resolves at the proxy for SOCKS5 already.
    if url.startswith("socks5h://"):
        url = "socks5://" + url[len("socks5h://"):]
    if not url.startswith(_PROXY_SCHEMES):
        raise HTTPException(
            400,
            "آدرس پروکسی باید با socks5:// یا http:// شروع شود. "
            "نمونه: socks5://user:pass@1.2.3.4:1080",
        )
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if not parsed.hostname:
        raise HTTPException(400, "آدرس پروکسی معتبر نیست - میزبان مشخص نشده است")
    if not parsed.port:
        raise HTTPException(
            400,
            "پورت پروکسی را هم بنویسید. نمونه: socks5://1.2.3.4:1080",
        )
    return url


def _unlinked_admin_ids(db: Session, raw: str | None) -> list[int]:
    """Ids in the bot's admin list with no AdminUser behind them.

    Surfaced in the settings response so the panel can warn where the field
    is edited. This used to be visible only by running a script over SSH,
    which means in practice it was visible to nobody.
    """
    from ..telegram_bot.config import parse_id_set

    ids = parse_id_set(raw or "")
    if not ids:
        return []
    linked = {
        row.telegram_id
        for row in db.query(models.AdminUser.telegram_id)
        .filter(models.AdminUser.telegram_id.in_(ids))
        .all()
    }
    return sorted(ids - linked)


def _response(row: models.BotSettings, db: Session | None = None) -> schemas.BotSettingsOut:
    status = runner.get_status()
    return schemas.BotSettingsOut(
        bot_token=row.bot_token or "",
        admin_ids=row.admin_ids or "",
        unlinked_admin_ids=_unlinked_admin_ids(db, row.admin_ids) if db is not None else [],
        approval_chat_ids=row.approval_chat_ids or "",
        enabled=bool(row.enabled),
        last_error=status.get("last_error") or row.last_error,
        running=bool(status.get("running")),
        bot_username=status.get("bot_username"),
        remote_mode=bool(row.remote_mode),
        remote_host=row.remote_host,
        remote_ssh_port=row.remote_ssh_port or 22,
        remote_ssh_username=row.remote_ssh_username or "root",
        remote_status=row.remote_status,
        remote_deployed_at=row.remote_deployed_at,
        customer_bot_enabled=row.customer_bot_enabled if row.customer_bot_enabled is not None else True,
        customer_menu_disabled_items=row.customer_menu_disabled_items or "",
        telegram_api_proxy_url=row.telegram_api_proxy_url or "",
        telegram_proxy_url=row.telegram_proxy_url or "",
        auto_approve_enabled=bool(row.auto_approve_enabled),
        auto_approve_ignore_hours=bool(row.auto_approve_ignore_hours),
        auto_approve_from_hour=row.auto_approve_from_hour if row.auto_approve_from_hour is not None else 9,
        auto_approve_to_hour=row.auto_approve_to_hour if row.auto_approve_to_hour is not None else 23,
        auto_approve_max_amount=row.auto_approve_max_amount or 0,
        auto_approve_returning_only=row.auto_approve_returning_only if row.auto_approve_returning_only is not None else True,
    )


@router.get("", response_model=schemas.BotSettingsOut)
def get_settings(db: Session = Depends(get_db), _s=_superadmin):
    return _response(_get_or_create(db), db)


@router.put("", response_model=schemas.BotSettingsOut)
def update_settings(payload: schemas.BotSettingsUpdate, db: Session = Depends(get_db), _s=_superadmin):
    row = _get_or_create(db)
    data = payload.model_dump(exclude_unset=True)
    if "telegram_proxy_url" in data:
        data["telegram_proxy_url"] = _validate_proxy_url(data["telegram_proxy_url"]) or None
    # Auto-approve values are clamped rather than rejected: an out-of-range
    # hour is a typo, and refusing the whole save would also throw away the
    # other settings the admin just edited. The clamp cannot widen the
    # window, only keep it inside a real 24h clock.
    for key in ("auto_approve_from_hour", "auto_approve_to_hour"):
        if data.get(key) is not None:
            data[key] = max(0, min(int(data[key]), 23))
    if data.get("auto_approve_max_amount") is not None:
        data["auto_approve_max_amount"] = max(0, int(data["auto_approve_max_amount"]))
    for k, v in data.items():
        setattr(row, k, v)
    row.last_error = None
    db.commit()
    db.refresh(row)

    # If the interactive bot is currently running on a remote server
    # (see routers/remote_bot.py), never start a second local poller -
    # Telegram only allows one getUpdates() poller per token and they'd
    # just fight each other. The admin has to explicitly "بازگرداندن به
    # همین سرور" first.
    if not row.remote_mode:
        runner.restart_bot(
            row.bot_token or "",
            parse_id_set(row.admin_ids or ""),
            parse_id_set(row.approval_chat_ids or ""),
            bool(row.enabled),
            customer_bot_enabled=row.customer_bot_enabled if row.customer_bot_enabled is not None else True,
        )
    return _response(row, db)


@router.post("/restart", response_model=schemas.BotSettingsOut)
def restart(db: Session = Depends(get_db), _s=_superadmin):
    """Re-applies the currently saved settings - handy to force a restart
    without changing anything (e.g. after a Telegram network hiccup)."""
    row = _get_or_create(db)
    if not row.remote_mode:
        runner.restart_bot(
            row.bot_token or "",
            parse_id_set(row.admin_ids or ""),
            parse_id_set(row.approval_chat_ids or ""),
            bool(row.enabled),
            customer_bot_enabled=row.customer_bot_enabled if row.customer_bot_enabled is not None else True,
        )
    return _response(row, db)


# ------------------------------------------------------- per-admin own bot
def _own_bot_response(admin: models.AdminUser) -> schemas.OwnBotSettingsOut:
    status = runner.get_admin_bot_status(admin.id)
    return schemas.OwnBotSettingsOut(
        bot_token=admin.own_bot_token or "",
        enabled=bool(admin.own_bot_enabled),
        running=bool(status.get("running")),
        last_error=status.get("last_error"),
        bot_username=status.get("bot_username"),
        telegram_id_linked=admin.telegram_id is not None,
    )


@router.get("/my-bot", response_model=schemas.OwnBotSettingsOut)
def get_my_bot(admin: models.AdminUser = Depends(get_current_admin)):
    _require_admin_tier(admin)
    return _own_bot_response(admin)


@router.put("/my-bot", response_model=schemas.OwnBotSettingsOut)
def update_my_bot(
    payload: schemas.OwnBotSettingsUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    """Saves this Admin's OWN bot token/enabled flag and (re)starts their
    dedicated bot instance right away - see telegram_bot/runner.py's
    start_admin_bot/restart_admin_bot. Fully independent of the shared/
    global bot above; nothing here ever touches the BotSettings row."""
    _require_admin_tier(admin)
    data = payload.model_dump(exclude_unset=True)
    if "bot_token" in data:
        token = (data["bot_token"] or "").strip()
        # Telegram permits exactly ONE getUpdates poller per token. Saving a
        # token that another admin (or the shared bot) already uses doesn't
        # create a second bot - the instances knock each other offline in a
        # loop and none of them receives anything. Caught here rather than
        # left to fail silently at runtime; see main.py's startup loop for
        # the matching guard on rows that predate this check.
        if token:
            clash = (
                db.query(models.AdminUser)
                .filter(models.AdminUser.own_bot_token == token, models.AdminUser.id != admin.id)
                .first()
            )
            if clash is not None:
                raise HTTPException(
                    400,
                    f"این توکن ربات هم‌اکنون برای ادمین «{clash.username}» ثبت شده است. "
                    "تلگرام اجازه‌ی اجرای همزمان یک توکن را نمی‌دهد - برای هر ادمین از @BotFather یک ربات جدا بسازید.",
                )
            shared = db.get(models.BotSettings, 1)
            if shared is not None and (shared.bot_token or "").strip() == token:
                raise HTTPException(
                    400,
                    "این توکن همان ربات مشترک پنل است. برای ربات اختصاصی باید از @BotFather یک ربات جدا بسازید.",
                )
        admin.own_bot_token = token or None
    if "enabled" in data:
        admin.own_bot_enabled = bool(data["enabled"])
    db.commit()
    db.refresh(admin)

    runner.restart_admin_bot(admin.id, admin.own_bot_token or "", admin.telegram_id, bool(admin.own_bot_enabled))
    return _own_bot_response(admin)


@router.post("/my-bot/restart", response_model=schemas.OwnBotSettingsOut)
def restart_my_bot(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    _require_admin_tier(admin)
    runner.restart_admin_bot(admin.id, admin.own_bot_token or "", admin.telegram_id, bool(admin.own_bot_enabled))
    return _own_bot_response(admin)
