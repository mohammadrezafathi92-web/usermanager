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
    already has the shared/global bot above, so only they're excluded."""
    if hierarchy.role(admin) == hierarchy.ROLE_SUPERADMIN:
        raise HTTPException(403, "این بخش برای ادمین اصلی در دسترس نیست - از تنظیمات ربات مشترک استفاده کنید")


def _get_or_create(db: Session) -> models.BotSettings:
    row = db.get(models.BotSettings, 1)
    if not row:
        row = models.BotSettings(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _response(row: models.BotSettings) -> schemas.BotSettingsOut:
    status = runner.get_status()
    return schemas.BotSettingsOut(
        bot_token=row.bot_token or "",
        admin_ids=row.admin_ids or "",
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
    )


@router.get("", response_model=schemas.BotSettingsOut)
def get_settings(db: Session = Depends(get_db), _s=_superadmin):
    return _response(_get_or_create(db))


@router.put("", response_model=schemas.BotSettingsOut)
def update_settings(payload: schemas.BotSettingsUpdate, db: Session = Depends(get_db), _s=_superadmin):
    row = _get_or_create(db)
    for k, v in payload.model_dump(exclude_unset=True).items():
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
    return _response(row)


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
    return _response(row)


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
        admin.own_bot_token = data["bot_token"] or None
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
