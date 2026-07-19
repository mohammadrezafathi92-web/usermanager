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
from ..deps import require_admin_or_above, require_superadmin, get_bot_api_key, get_current_admin
from ..services import backup as backup_service
from ..services import local_deploy
from ..services import hierarchy

# Panel-wide (single PanelSettings row, id=1) - payment/checkout info,
# support contact, referral/loyalty config, panel port, HA config all
# affect every Admin's and Seller's customers at once, so this whole
# router is superadmin/level-2-Admin only (require_admin_or_above) - a
# level-3 Seller is structurally blocked, not just checkbox-gated. See
# permissions.py's module docstring for the full reasoning.
router = APIRouter(prefix="/api/settings", tags=["settings"], dependencies=[Depends(require_admin_or_above)])


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
    """Changes the port EVERYONE reaches the panel on (superadmin-only) by
    editing docker-compose.yml and recreating the frontend container -
    fully local now, over the docker socket mounted into this container
    (see services/local_deploy.py's module docstring for how/why). No SSH
    details or password needed any more - just the new port number."""
    row = _get_or_create(db)
    current_port = row.panel_web_port or 80
    try:
        log = local_deploy.change_panel_port_local(
            current_port=current_port,
            new_port=payload.new_port,
        )
    except local_deploy.DeployError as exc:
        row.panel_port_status = f"خطا: {exc}"
        db.commit()
        raise HTTPException(400, str(exc))

    row.panel_web_port = payload.new_port
    row.panel_port_status = log
    row.panel_port_changed_at = dt.datetime.utcnow()
    db.commit()
    return {"ok": True, "message": log}


# ---------------------------------------------------------------------------
# Per-admin own card-to-card payment info (3-tier hierarchy - see
# AdminUser.own_payment_card_number's docstring in models.py). A SEPARATE
# router (not more routes bolted onto `router` above) because it needs
# different auth: `router`'s whole point is superadmin/level-2-Admin-only
# since it edits the ONE shared PanelSettings row - but a level-3 Seller
# absolutely must be able to set their OWN card here (their own bot shows
# it to their own customers), so this is gated to any logged-in admin
# EXCEPT superadmin instead (mirrors telegram_bot_settings.py's /my-bot
# and _require_admin_tier - a superadmin edits the global row directly via
# `router` above instead, which also backs the shared bot).
my_payment_router = APIRouter(prefix="/api/settings/my-payment", tags=["settings"])


def _require_not_superadmin(admin: models.AdminUser) -> None:
    if hierarchy.role(admin) == hierarchy.ROLE_SUPERADMIN:
        raise HTTPException(403, "این بخش برای ادمین اصلی در دسترس نیست - از تنظیمات پرداخت مشترک استفاده کنید")


@my_payment_router.get("", response_model=schemas.OwnPaymentSettingsOut)
def get_my_payment(admin: models.AdminUser = Depends(get_current_admin)):
    _require_not_superadmin(admin)
    return schemas.OwnPaymentSettingsOut(
        payment_card_number=admin.own_payment_card_number or "",
        payment_card_holder=admin.own_payment_card_holder or "",
        payment_instructions=admin.own_payment_instructions or "",
        topup_presets=admin.own_topup_presets or "",
    )


@my_payment_router.put("", response_model=schemas.OwnPaymentSettingsOut)
def update_my_payment(
    payload: schemas.OwnPaymentSettingsUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    """Blank/whitespace-only values are stored as NULL (not an empty
    string) so get_payment_info's per-field fallback to the global
    PanelSettings row (see routers/bot.py) actually kicks in - an empty
    string would otherwise "win" over the fallback and show the customer
    nothing at all instead of the panel-wide default."""
    _require_not_superadmin(admin)
    data = payload.model_dump(exclude_unset=True)
    if "payment_card_number" in data:
        admin.own_payment_card_number = (data["payment_card_number"] or "").strip() or None
    if "payment_card_holder" in data:
        admin.own_payment_card_holder = (data["payment_card_holder"] or "").strip() or None
    if "payment_instructions" in data:
        admin.own_payment_instructions = (data["payment_instructions"] or "").strip() or None
    if "topup_presets" in data:
        admin.own_topup_presets = (data["topup_presets"] or "").strip() or None
    db.commit()
    db.refresh(admin)
    return schemas.OwnPaymentSettingsOut(
        payment_card_number=admin.own_payment_card_number or "",
        payment_card_holder=admin.own_payment_card_holder or "",
        payment_instructions=admin.own_payment_instructions or "",
        topup_presets=admin.own_topup_presets or "",
    )


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
