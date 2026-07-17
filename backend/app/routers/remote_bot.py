"""Admin-only endpoints for deploying/stopping the INTERACTIVE Telegram bot
on a second ("remote") server - "نصب ربات روی سرور دیگر" in Settings. The
actual SSH work happens in services/remote_deploy.py; this router just
wires it to the BotSettings row and the local in-process poller.

The SSH password only ever lives in the request body for the duration of
one call (deploy or stop) - it is never written to the database."""
import datetime as dt

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import require_permission
from ..services import remote_deploy
from ..services.keys import generate_api_key
from ..telegram_bot import runner as telegram_bot_runner
from ..telegram_bot.config import parse_id_set
from .telegram_bot_settings import _get_or_create as _get_or_create_bot_settings, _response as _bot_settings_response

router = APIRouter(prefix="/api/remote-bot", tags=["remote-bot"], dependencies=[Depends(require_permission("manage_bot_settings"))])


@router.get("/status", response_model=schemas.BotSettingsOut)
def status(db: Session = Depends(get_db)):
    return _bot_settings_response(_get_or_create_bot_settings(db))


@router.post("/deploy", response_model=schemas.BotSettingsOut)
def deploy(payload: schemas.RemoteBotDeployRequest, db: Session = Depends(get_db)):
    row = _get_or_create_bot_settings(db)
    if not row.bot_token or not row.admin_ids:
        raise HTTPException(400, "اول از بخش «ربات تلگرام» توکن و آیدی عددی ادمین را تنظیم و ذخیره کنید")

    panel_api_url = (payload.panel_public_url or "").strip()
    if not panel_api_url:
        host = settings.panel_public_host
        if not host:
            raise HTTPException(
                400,
                "آدرسی از این پنل که از سرور دوم قابل دسترسی باشد وارد کنید (مثلا http://IP-همین-سرور:8000)",
            )
        panel_api_url = f"http://{host}:8000"
    # remote_bridge.py's RemoteBridge appends bare paths like "/nodes",
    # "/users" etc. straight onto this base URL with no prefix of its own -
    # it expects PANEL_API_URL to already point AT routers/bot.py's mount
    # point (prefix="/api/bot"), not just the panel's host:port. The admin
    # only ever types/sees "http://IP:8000" (that's what the Settings UI's
    # placeholder shows), so without this, every single call the remote bot
    # makes 404s ("Not Found") and it looks like it can't see the database
    # at all even though SSH/deploy itself succeeded fine.
    panel_api_url = panel_api_url.rstrip("/")
    if not panel_api_url.endswith("/api/bot"):
        panel_api_url += "/api/bot"

    # Dedicated API key for this remote bot, created fresh on every deploy
    # so re-deploying (e.g. moving to a different server) doesn't reuse an
    # old key - the previous one (if any) is disabled, not deleted, in
    # case something else still references it.
    if row.remote_api_key_id:
        old_key = db.get(models.ApiKey, row.remote_api_key_id)
        if old_key:
            old_key.enabled = False

    api_key = models.ApiKey(label=f"ربات روی سرور دور ({payload.host})", key=generate_api_key())
    db.add(api_key)
    db.flush()

    try:
        log = remote_deploy.deploy(
            host=payload.host,
            ssh_port=payload.ssh_port,
            ssh_username=payload.ssh_username,
            ssh_password=payload.ssh_password,
            panel_api_url=panel_api_url,
            panel_api_key=api_key.key,
            bot_token=row.bot_token,
            admin_ids=row.admin_ids or "",
            approval_chat_ids=row.approval_chat_ids or "",
        )
    except remote_deploy.DeployError as exc:
        db.rollback()
        detail = str(exc)
        if exc.log:
            detail += "\n\n" + exc.log
        raise HTTPException(400, detail) from exc

    # Success - stop the LOCAL poller. Telegram only allows one
    # getUpdates() poller per bot token, so from this point on the remote
    # instance is the only one actually talking to Telegram; this server
    # keeps sending outbound notifications/backups independently (see
    # telegram_bot/runner.py's send_message_sync/send_document_sync).
    telegram_bot_runner.stop_bot()
    row.enabled = False
    row.remote_mode = True
    row.remote_host = payload.host
    row.remote_ssh_port = payload.ssh_port
    row.remote_ssh_username = payload.ssh_username
    row.remote_api_key_id = api_key.id
    row.remote_status = log
    row.remote_deployed_at = dt.datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _bot_settings_response(row)


@router.post("/stop", response_model=schemas.BotSettingsOut)
def stop(payload: schemas.RemoteBotStopRequest, db: Session = Depends(get_db)):
    """Brings the remote bot container down and, if a token/admin ids are
    still configured, resumes running the bot in-process on this server."""
    row = _get_or_create_bot_settings(db)
    if not row.remote_mode or not row.remote_host:
        raise HTTPException(400, "در حال حاضر ربات روی سرور دیگری اجرا نمی‌شود")

    try:
        remote_deploy.stop(
            row.remote_host,
            row.remote_ssh_port or 22,
            row.remote_ssh_username or "root",
            payload.ssh_password,
        )
    except remote_deploy.DeployError as exc:
        raise HTTPException(400, str(exc)) from exc

    row.remote_mode = False
    row.remote_status = "متوقف شد و به سرور اصلی بازگشت."
    if row.bot_token and row.admin_ids:
        row.enabled = True
    db.commit()
    db.refresh(row)

    if row.enabled:
        telegram_bot_runner.restart_bot(
            row.bot_token or "",
            parse_id_set(row.admin_ids or ""),
            parse_id_set(row.approval_chat_ids or ""),
            True,
            customer_bot_enabled=row.customer_bot_enabled if row.customer_bot_enabled is not None else True,
        )
    return _bot_settings_response(row)
