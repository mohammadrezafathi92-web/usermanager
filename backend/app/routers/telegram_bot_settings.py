"""Admin-only settings for the built-in Telegram bot (token, admin ids) -
lets the admin fully configure and run the bot from the web UI, with no
.env file or SSH access needed. Saving restarts the bot's in-process
polling loop (see app/telegram_bot/runner.py) with the new settings right
away."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_permission
from ..telegram_bot import runner
from ..telegram_bot.config import parse_id_set

router = APIRouter(prefix="/api/telegram-bot", tags=["telegram-bot"], dependencies=[Depends(require_permission("manage_settings"))])


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
    )


@router.get("", response_model=schemas.BotSettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return _response(_get_or_create(db))


@router.put("", response_model=schemas.BotSettingsOut)
def update_settings(payload: schemas.BotSettingsUpdate, db: Session = Depends(get_db)):
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
def restart(db: Session = Depends(get_db)):
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
