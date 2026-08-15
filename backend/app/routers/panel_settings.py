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
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

import datetime as dt
import secrets

from .. import models, schemas
from ..services import jalali
from ..database import get_db
from ..deps import require_admin_or_above, require_superadmin, get_current_admin, require_confirm_password
from ..services import backup as backup_service
from ..services import local_deploy
from ..services import hierarchy
from ..services import payment_cards as payment_cards_service

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


def _settings_out(db: Session, row: models.PanelSettings) -> schemas.PanelSettingsOut:
    """PaymentCardOut.payment_cards isn't a real column/relationship on
    PanelSettings - populated here from the global pool (owner_admin_id
    IS NULL) instead of relying on from_attributes to find it."""
    out = schemas.PanelSettingsOut.model_validate(row)
    out.payment_cards = [
        schemas.PaymentCardOut.model_validate(c) for c in payment_cards_service.list_cards(db, None)
    ]
    return out


def _is_private_host(url: str) -> bool:
    """True for loopback/RFC1918/link-local hosts, where plain HTTP between
    two servers is acceptable. A hostname that does not parse as an IP is
    treated as public - guessing at DNS here would be worse than being
    strict."""
    import ipaddress
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").strip()
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local

@router.get("", response_model=schemas.PanelSettingsOut)
def get_settings(db: Session = Depends(get_db)):
    return _settings_out(db, _get_or_create(db))


@router.put("", response_model=schemas.PanelSettingsOut)
def update_settings(
    payload: schemas.PanelSettingsUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    row = _get_or_create(db)
    data = payload.model_dump(exclude_unset=True)

    # HA settings are superadmin-only, even though this whole router is
    # otherwise open to a level-2 Admin.
    #
    # /api/ha/snapshot serves a gzip of the ENTIRE database - every admin's
    # password hash, every tenant's node SSH/API credentials, every bot
    # token - and it authenticates against PanelSettings.ha_peer_api_key.
    # That key is a field on this very endpoint. So a level-2 Admin (or a
    # stolen level-2 session) could simply PUT a key of their choosing and
    # then fetch the whole database with it. No peer server, no failover, no
    # exploit needed - just two ordinary API calls.
    ha_fields = {k: v for k, v in data.items() if k.startswith("ha_")}
    if ha_fields and not admin.is_superadmin:
        raise HTTPException(403, "تنظیمات HA فقط توسط سوپرادمین قابل تغییر است")

    if data.get("ha_peer_url"):
        # Admins commonly type just "IP:8000" - requests then raises
        # MissingSchema on every health-check/pull, which ha_tick can only
        # see as "peer unreachable", silently leading to a false-alarm
        # auto-failover ~100s later. Auto-prepend http:// so a bare
        # host:port still works instead of failing in a confusing way.
        url = data["ha_peer_url"].strip()
        if url and "://" not in url:
            url = f"http://{url}"
        # Plain HTTP is refused for a peer reachable over the public
        # internet: the HA key travels in that request's header and the
        # response is the entire database, both in clear text on the wire.
        # A private/loopback address is still allowed, because two servers
        # on the same private network is a legitimate setup and forcing TLS
        # there would mean certificates for RFC1918 addresses - friction
        # with no attacker to stop.
        if url.startswith("http://") and not _is_private_host(url):
            raise HTTPException(
                400,
                "برای سرور HA روی اینترنت باید از https استفاده شود - "
                "کلید HA و کل دیتابیس از این مسیر رد می‌شوند. "
                "برای شبکه‌ی داخلی، آدرس خصوصی (۱۹۲.۱۶۸.x.x، ۱۰.x.x.x یا localhost) مجاز است.",
            )
        data["ha_peer_url"] = url
    for k, v in data.items():
        setattr(row, k, v)
    db.commit()
    db.refresh(row)
    # services/jalali.py caches this offset in a module global (it is read
    # per rendered date, far too often for a DB lookup each time), so it has
    # to be told when the stored value changes - otherwise a new timezone
    # would only take effect at the next restart.
    jalali.set_display_offset(row.display_utc_offset_minutes)
    return _settings_out(db, row)


# ---------------------------------------------------------------------------
# Global payment card pool (چند شماره کارت) - see services/payment_cards.py's
# module docstring for the manual/rotate/threshold modes. Card-holder for
# this pool is the shared/global bot (superadmin's own) - a level-2 Admin/
# level-3 Seller manages their OWN pool instead, via my_payment_router
# below. Deliberately just more routes on `router` (already superadmin/
# level-2-Admin-only via require_admin_or_above) rather than a separate
# router, same reasoning as change-port above.
@router.get("/payment-cards", response_model=list[schemas.PaymentCardOut])
def list_payment_cards(db: Session = Depends(get_db)):
    return payment_cards_service.list_cards(db, None)


@router.post("/payment-cards", response_model=schemas.PaymentCardOut)
def create_payment_card(payload: schemas.PaymentCardCreate, db: Session = Depends(get_db)):
    was_empty = not payment_cards_service.list_cards(db, None)
    card = models.PaymentCard(owner_admin_id=None, **payload.model_dump())
    db.add(card)
    db.flush()
    if was_empty:
        # First card ever added to an empty pool - make it the active one
        # right away instead of leaving active_payment_card_id null (which
        # would otherwise silently keep showing the legacy single
        # payment_card_number field until the admin remembers to activate
        # something).
        row = _get_or_create(db)
        row.active_payment_card_id = card.id
    db.commit()
    db.refresh(card)
    return card


@router.put("/payment-cards/{card_id}", response_model=schemas.PaymentCardOut)
def update_payment_card(card_id: int, payload: schemas.PaymentCardUpdate, db: Session = Depends(get_db)):
    card = db.get(models.PaymentCard, card_id)
    if not card or card.owner_admin_id is not None:
        raise HTTPException(404, "کارت پیدا نشد")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(card, k, v)
    db.commit()
    db.refresh(card)
    return card


@router.delete("/payment-cards/{card_id}")
def delete_payment_card(card_id: int, db: Session = Depends(get_db), _confirm=Depends(require_confirm_password)):
    card = db.get(models.PaymentCard, card_id)
    if not card or card.owner_admin_id is not None:
        raise HTTPException(404, "کارت پیدا نشد")
    row = _get_or_create(db)
    db.delete(card)
    db.flush()
    if row.active_payment_card_id == card_id:
        # Was the active card - fall back to whatever's left in the pool
        # (None if this was the last card) instead of leaving a dangling
        # pointer to a deleted row.
        remaining = payment_cards_service.list_cards(db, None)
        row.active_payment_card_id = remaining[0].id if remaining else None
    db.commit()
    return {"ok": True}


@router.post("/payment-cards/{card_id}/activate", response_model=schemas.PanelSettingsOut)
def activate_payment_card(card_id: int, db: Session = Depends(get_db)):
    """Manual card selection - sets card_id as the one shown to customers
    right now. Meaningful in "manual" and "threshold" mode; harmless
    (just overwritten on the next view) in "rotate" mode."""
    card = db.get(models.PaymentCard, card_id)
    if not card or card.owner_admin_id is not None:
        raise HTTPException(404, "کارت پیدا نشد")
    row = _get_or_create(db)
    row.active_payment_card_id = card.id
    db.commit()
    db.refresh(row)
    return _settings_out(db, row)


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


def _own_payment_out(db: Session, admin: models.AdminUser) -> schemas.OwnPaymentSettingsOut:
    return schemas.OwnPaymentSettingsOut(
        payment_card_number=admin.own_payment_card_number or "",
        payment_card_holder=admin.own_payment_card_holder or "",
        payment_instructions=admin.own_payment_instructions or "",
        topup_presets=admin.own_topup_presets or "",
        payment_card_mode=admin.own_payment_card_mode or "manual",
        active_payment_card_id=admin.own_active_payment_card_id,
        payment_card_switch_threshold=admin.own_payment_card_switch_threshold,
        payment_cards=[
            schemas.PaymentCardOut.model_validate(c) for c in payment_cards_service.list_cards(db, admin.id)
        ],
    )


@my_payment_router.get("", response_model=schemas.OwnPaymentSettingsOut)
def get_my_payment(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    _require_not_superadmin(admin)
    return _own_payment_out(db, admin)


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
    if "payment_card_mode" in data:
        admin.own_payment_card_mode = data["payment_card_mode"] or "manual"
    if "active_payment_card_id" in data:
        admin.own_active_payment_card_id = data["active_payment_card_id"]
    if "payment_card_switch_threshold" in data:
        admin.own_payment_card_switch_threshold = data["payment_card_switch_threshold"]
    db.commit()
    db.refresh(admin)
    return _own_payment_out(db, admin)


# ------------------------------------------------------- per-admin card pool
# Same shape as the global /payment-cards routes above, scoped to this
# admin's own pool (owner_admin_id=admin.id) instead of the global one.
@my_payment_router.get("/cards", response_model=list[schemas.PaymentCardOut])
def list_my_payment_cards(admin: models.AdminUser = Depends(get_current_admin), db: Session = Depends(get_db)):
    _require_not_superadmin(admin)
    return payment_cards_service.list_cards(db, admin.id)


@my_payment_router.post("/cards", response_model=schemas.PaymentCardOut)
def create_my_payment_card(
    payload: schemas.PaymentCardCreate,
    admin: models.AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    _require_not_superadmin(admin)
    was_empty = not payment_cards_service.list_cards(db, admin.id)
    card = models.PaymentCard(owner_admin_id=admin.id, **payload.model_dump())
    db.add(card)
    db.flush()
    if was_empty:
        admin.own_active_payment_card_id = card.id
    db.commit()
    db.refresh(card)
    return card


def _get_own_card_or_404(db: Session, admin: models.AdminUser, card_id: int) -> models.PaymentCard:
    card = db.get(models.PaymentCard, card_id)
    if not card or card.owner_admin_id != admin.id:
        raise HTTPException(404, "کارت پیدا نشد")
    return card


@my_payment_router.put("/cards/{card_id}", response_model=schemas.PaymentCardOut)
def update_my_payment_card(
    card_id: int,
    payload: schemas.PaymentCardUpdate,
    admin: models.AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    _require_not_superadmin(admin)
    card = _get_own_card_or_404(db, admin, card_id)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(card, k, v)
    db.commit()
    db.refresh(card)
    return card


@my_payment_router.delete("/cards/{card_id}")
def delete_my_payment_card(
    card_id: int, admin: models.AdminUser = Depends(get_current_admin), db: Session = Depends(get_db),
    _confirm=Depends(require_confirm_password),
):
    _require_not_superadmin(admin)
    card = _get_own_card_or_404(db, admin, card_id)
    db.delete(card)
    db.flush()
    if admin.own_active_payment_card_id == card_id:
        remaining = payment_cards_service.list_cards(db, admin.id)
        admin.own_active_payment_card_id = remaining[0].id if remaining else None
    db.commit()
    return {"ok": True}


@my_payment_router.post("/cards/{card_id}/activate", response_model=schemas.OwnPaymentSettingsOut)
def activate_my_payment_card(
    card_id: int, admin: models.AdminUser = Depends(get_current_admin), db: Session = Depends(get_db),
):
    _require_not_superadmin(admin)
    card = _get_own_card_or_404(db, admin, card_id)
    admin.own_active_payment_card_id = card.id
    db.commit()
    db.refresh(admin)
    return _own_payment_out(db, admin)


# ---------------------------------------------------------------------------
# مورد ۱۰: HA / near-real-time replication به سرور دوم. See this file's
# module docstring for why this is a separate router with different auth.
ha_router = APIRouter(prefix="/api/ha", tags=["ha"])


def _require_ha_peer_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> None:
    """Auth for /api/ha/snapshot - deliberately NOT get_bot_api_key.
    get_bot_api_key accepts ANY enabled ApiKey row, but ApiKey is the same
    table used for third-party bot integrations and the auto-generated key
    for a remote Telegram bot deployment (see routers/remote_bot.py) - any
    one of those keys used to also work here, letting a low-trust
    integration credential download a full gzip snapshot of the ENTIRE
    database (every AdminUser's hashed password, every tenant's node SSH/
    API credentials, even ha_peer_api_key itself). This endpoint must only
    ever accept the ONE specific shared secret configured for HA
    replication (PanelSettings.ha_peer_api_key - manually set to the same
    value on both servers, see models.py's docstring), nothing else."""
    row = db.get(models.PanelSettings, 1)
    expected = (row.ha_peer_api_key or "") if row else ""
    # compare_digest, not ==: a plain comparison returns as soon as two bytes
    # differ, so response time leaks how much of the key was guessed right.
    # This is the one endpoint where that matters - it hands out the whole
    # database - and it is called unattended, so nobody would notice the
    # thousands of probes a timing attack needs.
    if not expected or not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="کلید HA نامعتبر است")


@ha_router.get("/snapshot")
def ha_snapshot(_: None = Depends(_require_ha_peer_key)):
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
