"""Panel-wide settings that aren't tied to a specific node - the card-to-
card payment info the sales bot shows customers at checkout, plus (مورد ۱۰)
HA / near-real-time replication config to a second server.

Also defines a second router, ha_router (near the bottom of this file):
the peer-facing endpoint a standby's ha_tick (main.py) polls every ~20s to
pull this server's latest DB snapshot, authenticated with the SAME
X-API-Key header the external bot API uses (get_bot_api_key) instead of an
admin JWT, since it's the PEER SERVER calling this unattended - not a
logged-in admin. See services/backup.py's create_snapshot_bytes/
ha_pull_and_apply/ha_healthcheck and main.py's ha_tick()/
_promote_to_active() for the rest of the flow."""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

import datetime as dt

from .. import models, schemas
from ..database import get_db
from ..deps import require_permission, require_superadmin, get_bot_api_key
from ..services import backup as backup_service
from ..services import remote_deploy
from ..services.remote_deploy import DeployError

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
    data = payload.model_dump(exclude_unset=True)
    if data.get("ha_peer_url"):
        # Admins commonly type just "IP:8000" - requests then raises
        # MissingSchema on every health-check/pull, which ha_tick can only
        # see as "peer unreachable", silently leading to a false-alarm
        # auto-failover ~100s later. Auto-prepend http:// so a bare
        # host:port still works instead of failing in a confusing way.
        url = data["ha_peer_url"].strip()
        if url and "://" not in url:
            url = f"http://{url}"
        data["ha_peer_url"] = url
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    return row


@router.post("/change-port", response_model=schemas.PanelPortChangeResult, dependencies=[Depends(require_superadmin)])
def change_panel_port(payload: schemas.PanelPortChangeRequest, db: Session = Depends(get_db)):
    """SSHes into the panel's own host (superadmin-only - this changes how
    EVERYONE reaches the panel) to edit docker-compose.yml's frontend port
    mapping and recreate that one container - see
    services/remote_deploy.py's change_panel_port for the actual commands.
    Requires panel_ssh_host/panel_project_dir to already be saved via PUT
    above; the SSH password is taken fresh from this request and never
    stored (same rule as the remote-bot deploy feature)."""
    row = _get_or_create(db)
    if not row.panel_ssh_host:
        raise HTTPException(400, "ابتدا آدرس SSH این سرور را در تنظیمات ذخیره کنید")

    current_port = row.panel_web_port or 80
    try:
        log = remote_deploy.change_panel_port(
            host=row.panel_ssh_host,
            ssh_port=row.panel_ssh_port or 22,
            ssh_username=row.panel_ssh_username or "root",
            ssh_password=payload.ssh_password,
            project_dir=row.panel_project_dir or "/root/usermanager",
            current_port=current_port,
            new_port=payload.new_port,
        )
    except DeployError as exc:
        row.panel_port_status = f"خطا: {exc}"
        db.commit()
        raise HTTPException(400, str(exc))

    row.panel_web_port = payload.new_port
    row.panel_port_status = log
    row.panel_port_changed_at = dt.datetime.utcnow()
    db.commit()
    return {"ok": True, "message": log}


# ---------------------------------------------------------------------------
# مورد ۱۰: HA / near-real-time replication به سرور دوم. See this file's
# module docstring for why this is a separate router with different auth.
ha_router = APIRouter(prefix="/api/ha", tags=["ha"])


@ha_router.get("/snapshot")
def ha_snapshot(_: models.ApiKey = Depends(get_bot_api_key)):
    data = backup_service.create_snapshot_bytes()
    return Response(content=data, media_type="application/gzip")


@ha_router.post("/resolve", dependencies=[Depends(require_superadmin)])
def ha_resolve(db: Session = Depends(get_db)):
    """Superadmin-only: manually acknowledges an auto-failover and clears
    ha_standby_active on THIS server, after the admin has checked both
    servers by hand and decided which one now holds the correct data.
    Deliberately does NOT auto-resume pulling from the peer or touch
    ha_mode/ha_enabled - resuming sync automatically here could silently
    overwrite whichever server the admin just decided to keep with stale
    data from the other one (see main.py's _promote_to_active docstring for
    the full split-brain reasoning)."""
    row = db.get(models.PanelSettings, 1)
    if not row:
        raise HTTPException(404, "تنظیمات پنل پیدا نشد")
    row.ha_standby_active = False
    row.ha_promoted_at = None
    row.ha_last_error = None
    db.commit()
    return {"ok": True}
