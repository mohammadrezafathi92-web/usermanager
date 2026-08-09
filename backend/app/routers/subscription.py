"""Public customer subscription panel - NO authentication, gated only by a
long unguessable per-user token in the URL (models.User.subscription_token,
see services/user_ops.py's ensure_subscription_token/regenerate_subscription_
token). Two things live here, both requested together by the admin:

  * GET /api/subscribe/{token}/info - JSON, human-readable page data (used
    by frontend/src/pages/Subscription.jsx): status, usage, expiry,
    balance, referral code, and each service's config/QR/copy data.
  * GET /api/subscribe/{token} - plain text, base64-encoded, newline-
    separated list of vless:// URIs - the "Subscribe" URL format V2rayNG/
    NekoBox/Clash-style apps expect, so a customer can paste this ONE link
    into their app and have every Xray service auto-imported/kept in sync
    on refresh. Kept under /api/ (rather than a bare /sub/{token}) purely
    so nginx.conf's existing `location /api/` proxy rule covers it with no
    changes needed - VPN client apps don't care about URL aesthetics.

Deliberately not under routers/users.py's `/api/users` router - that one
requires get_current_admin for the whole router (see its APIRouter(...,
dependencies=[Depends(get_current_admin)]) line), which this must NOT have."""
import base64

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..services import user_ops

router = APIRouter(prefix="/api/subscribe", tags=["subscription"])


def _get_user_by_token(token: str, db: Session) -> models.User:
    user = db.query(models.User).filter(models.User.subscription_token == token).first()
    if not user:
        raise HTTPException(404, "Not found")
    return user


@router.get("/{token}/info", response_model=schemas.SubscriptionInfo)
def get_subscription_info(token: str, db: Session = Depends(get_db)):
    user = _get_user_by_token(token, db)
    connections = user_ops.build_subscription_connections(user)
    return schemas.SubscriptionInfo(
        username=user.username,
        full_name=user.full_name,
        status=user.status,
        total_quota_bytes=user.total_quota_bytes,
        used_bytes=user.used_bytes,
        remaining_bytes=user.remaining_bytes,
        expire_at=user.expire_at,
        balance=user.balance,
        referral_code=user.referral_code,
        connections=connections,
    )


@router.get("/{token}/config/{connection_id}")
def download_connection_config(token: str, connection_id: int, db: Session = Depends(get_db)):
    """Downloadable config FILE for one service - what the QR code under
    each service on the subscription page points at, so scanning it with a
    phone camera downloads the ready-to-import file instead of dumping raw
    config text on screen (panel owner's request, 2026-08-09).

    For OpenVPN this is the package's admin-uploaded .ovpn template with
    only this customer's credentials injected (see link_builder.
    render_ovpn_template); for every other type it's the same human-
    readable config text/link the page already shows, as a .txt/.conf."""
    user = _get_user_by_token(token, db)
    conn = db.get(models.Connection, connection_id)
    if not conn or conn.user_id != user.id:
        raise HTTPException(404, "Not found")
    try:
        share = user_ops.get_connection_share(conn)
    except Exception:
        raise HTTPException(400, "config unavailable")

    if conn.type == models.ConnectionType.openvpn and share.get("ovpn_file"):
        body, ext, media = share["ovpn_file"], "ovpn", "application/x-openvpn-profile"
    elif conn.type == models.ConnectionType.wireguard and share.get("config_text"):
        body, ext, media = share["config_text"], "conf", "text/plain; charset=utf-8"
    else:
        body = share.get("link") or share.get("config_text") or ""
        ext, media = "txt", "text/plain; charset=utf-8"
    if not body:
        raise HTTPException(404, "config unavailable")

    filename = f"{user.username}-{conn.type.value}-{conn.id}.{ext}"
    return PlainTextResponse(
        body, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{token}")
def get_subscription_app_import(token: str, db: Session = Depends(get_db)):
    """The actual "Subscribe" URL an app like V2rayNG is pointed at -
    V2Ray/Xray services ONLY (confirmed with the panel owner 2026-08-09:
    this link is *the* V2Ray subscription link). Other protocol types
    (WireGuard/OpenVPN/L2TP/IKEv2/SSTP) aren't representable in this
    format at all and are skipped - they're delivered as downloadable
    config files instead (see download_connection_config above)."""
    user = _get_user_by_token(token, db)
    links = []
    for conn in user.connections:
        if conn.type != models.ConnectionType.xray:
            continue
        try:
            share = user_ops.get_connection_share(conn)
        except Exception:
            continue
        if share.get("link"):
            links.append(share["link"])
    body = "\n".join(links)
    encoded = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return PlainTextResponse(encoded)
