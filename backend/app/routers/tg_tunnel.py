"""«تونل تلگرام» - set up, from the panel, the WireGuard tunnel the bot
reaches Telegram through.

The panel already holds every MikroTik node's credentials and already
creates WireGuard peers on them for customers, so both ends of this tunnel
can be built from one button: generate a keypair here, register the peer
there, bring the interface up, route Telegram's ranges into it.

What this endpoint deliberately does NOT do is route anything else. See
services/wg_tunnel's module docstring: the panel's default route stays
where it is, so a broken tunnel costs the bot its connection and nothing
else. That constraint is the reason this is safe to press on a live panel.

The private key is generated here and never leaves the database. No
response in this module contains it - only the public key, which is what
the far end needs.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import require_superadmin
from ..services import user_ops, wg_tunnel
from ..services.mikrotik_client import MikrotikClient, MikrotikError

logger = logging.getLogger("tg_tunnel")

router = APIRouter(prefix="/api/telegram-tunnel", tags=["telegram-tunnel"],
                   dependencies=[Depends(require_superadmin)])

# The panel provisions itself as an ordinary customer under this username.
#
# Not a hand-rolled peer: services/user_ops.provision_wireguard is the exact
# function that serves paying customers, so the address comes from the node's
# real pool, the interface address and peer entry are written the same way,
# and every NAT rule, routing mark and firewall policy the node already has
# for its customers applies without a single line being added. Anything the
# panel did differently was one more thing that could be - and was - wrong.
TUNNEL_USERNAME = "panel-telegram-tunnel"


class SetupIn(BaseModel):
    node_id: int
    # Which WireGuard interface on the node to attach to. Nodes usually
    # already have one serving customers; reusing it means no new listen
    # port has to be opened anywhere.
    wg_interface: Optional[str] = None
    address: Optional[str] = None          # panel side, e.g. 10.77.0.2/30
    peer_address: Optional[str] = None     # node side, e.g. 10.77.0.1/30
    endpoint_port: Optional[int] = None


class TunnelOut(BaseModel):
    configured: bool
    enabled: bool
    node_id: Optional[int]
    node_name: Optional[str]
    interface_name: str
    public_key: Optional[str]
    address: Optional[str]
    peer_endpoint: Optional[str]
    allowed_ips: list[str]
    cidrs_updated_at: Optional[dt.datetime]
    last_error: Optional[str]
    # Live state, not stored - see services/wg_tunnel.status.
    interface_up: bool
    handshake_age_s: Optional[int]
    rx_bytes: int
    tx_bytes: int


def _row(db: Session) -> models.TelegramTunnel:
    row = db.get(models.TelegramTunnel, 1)
    if not row:
        row = models.TelegramTunnel(id=1)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


def _out(db: Session, row: models.TelegramTunnel) -> TunnelOut:
    st = wg_tunnel.status(row.interface_name or "wg-tg")
    node = db.get(models.Node, row.node_id) if row.node_id else None
    return TunnelOut(
        configured=bool(row.private_key and row.peer_public_key),
        enabled=bool(row.enabled),
        node_id=row.node_id,
        node_name=node.name if node else None,
        interface_name=row.interface_name or "wg-tg",
        public_key=row.public_key,
        address=row.address,
        peer_endpoint=row.peer_endpoint,
        allowed_ips=wg_tunnel.parse_cidrs(row.allowed_ips),
        cidrs_updated_at=row.cidrs_updated_at,
        last_error=row.last_error,
        interface_up=bool(st["exists"]),
        handshake_age_s=st["handshake_age_s"],
        rx_bytes=st["rx"],
        tx_bytes=st["tx"],
    )


@router.get("", response_model=TunnelOut)
def get_tunnel(db: Session = Depends(get_db)):
    return _out(db, _row(db))


@router.get("/nodes")
def candidate_nodes(db: Session = Depends(get_db)):
    """MikroTik nodes that could terminate the tunnel."""
    rows = (
        db.query(models.Node)
        .filter(models.Node.mt_host.isnot(None), models.Node.mt_host != "")
        .order_by(models.Node.name)
        .all()
    )
    return [
        {"id": n.id, "name": n.name, "host": n.mt_host,
         "wg_interface": n.mt_wireguard_interface or "wireguard1"}
        for n in rows
    ]


@router.post("/setup", response_model=TunnelOut)
def setup(payload: SetupIn, db: Session = Depends(get_db)):
    """Builds both ends. Order matters and is the point of this function.

    The node is configured FIRST and the local interface only afterwards.
    If the router refuses - wrong credentials, missing interface, no
    reachable API - nothing has been changed on this side and the bot keeps
    running exactly as it did. Doing it the other way round would leave a
    local interface routing Telegram into a tunnel whose far end never
    agreed to carry it, and the bot would go dark.
    """
    node = db.get(models.Node, payload.node_id)
    if node is None:
        raise HTTPException(404, "نود پیدا نشد")
    if not (node.mt_host or "").strip():
        raise HTTPException(400, "این نود میکروتیک نیست - تونل فقط روی نودهای میکروتیک ساخته می‌شود")

    row = _row(db)
    log: list[str] = []

    tunnel_user = db.query(models.User).filter(models.User.username == TUNNEL_USERNAME).first()
    if tunnel_user is None:
        tunnel_user = models.User(
            username=TUNNEL_USERNAME,
            full_name="اتصال داخلی پنل برای دسترسی ربات به تلگرام",
        )
        db.add(tunnel_user)
        db.commit()
        db.refresh(tunnel_user)
        log.append("کاربر داخلی تونل ساخته شد")

    # Old connections first. provision_wireguard reserves a NEW address every
    # time, so skipping this would leave a peer and a reserved IP behind on
    # the node for every retry - and a day of retries is exactly what this
    # feature has already had.
    stale = (
        db.query(models.Connection)
        .filter(models.Connection.user_id == tunnel_user.id)
        .all()
    )
    for conn in stale:
        try:
            user_ops.delete_connection(db, conn)
        except Exception:  # noqa: BLE001 - a stale peer is not worth failing over
            logger.warning("حذف اتصال قبلی تونل ناموفق بود (id=%s)", conn.id)
    if stale:
        log.append(f"{len(stale)} اتصال قبلی تونل حذف شد")

    try:
        conn = user_ops.provision_wireguard(db, tunnel_user, node)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"ساخت اتصال وایرگارد روی نود «{node.name}» ناموفق بود: {exc}")

    log.append(f"اتصال وایرگارد از همان مسیر مشتریان ساخته شد ({conn.wg_client_address})")

    try:
        with MikrotikClient.for_node(node) as client:
            peer_public = client.get_public_key(node.mt_wireguard_interface)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"خواندن کلید عمومی نود ناموفق بود: {exc}")
    if not peer_public:
        raise HTTPException(400, f"اینترفیس «{node.mt_wireguard_interface}» روی نود کلید عمومی ندارد")

    row.node_id = node.id
    row.interface_name = "wg-tg"
    row.private_key = conn.wg_private_key
    row.public_key = conn.wg_public_key
    row.address = conn.wg_client_address
    row.peer_public_key = peer_public
    row.peer_endpoint = (
        f"{node.mt_endpoint_host or node.mt_host}:{node.mt_endpoint_port or 13231}"
    )
    if not (row.allowed_ips or "").strip():
        row.allowed_ips = ",".join(wg_tunnel.DEFAULT_CIDRS)
    row.last_error = None

    try:
        log += wg_tunnel.up(row)
        row.enabled = True
    except wg_tunnel.TunnelError as exc:
        row.enabled = False
        row.last_error = f"{exc.message} | {exc.detail}"[:2000]
        db.commit()
        raise HTTPException(400, f"{exc.message}\n{exc.detail}")

    db.commit()
    db.refresh(row)

    ok, detail = wg_tunnel.test_telegram()
    if not ok:
        # Not an error: the peer may need a moment, or the node may not NAT
        # this address yet. Reported so the admin knows the tunnel is up but
        # not yet carrying traffic, instead of assuming it is finished.
        log.append(f"هشدار - تونل بالا آمد ولی تلگرام هنوز جواب نمی‌دهد ({detail}). "
                   "روی نود بررسی کنید که ترافیک این آدرس NAT می‌شود.")
    else:
        log.append(f"تست تلگرام موفق: {detail}")

    out = _out(db, row)
    return out.model_copy(update={"last_error": None if ok else "\n".join(log[-1:])})


_PEER_COMMENT = "netcip-telegram-tunnel"


@router.post("/up", response_model=TunnelOut)
def bring_up(db: Session = Depends(get_db)):
    row = _row(db)
    try:
        wg_tunnel.up(row)
    except wg_tunnel.TunnelError as exc:
        row.last_error = f"{exc.message} | {exc.detail}"[:2000]
        db.commit()
        raise HTTPException(400, f"{exc.message}\n{exc.detail}")
    row.enabled = True
    row.last_error = None
    db.commit()
    return _out(db, row)


@router.post("/down", response_model=TunnelOut)
def bring_down(db: Session = Depends(get_db)):
    row = _row(db)
    wg_tunnel.down(row.interface_name or "wg-tg")
    row.enabled = False
    db.commit()
    return _out(db, row)


@router.post("/test")
def test(db: Session = Depends(get_db)):
    """Full diagnosis, not a single probe.

    A bare pass/fail on one HTTPS request tells the admin the bot is broken
    and nothing about where. wg_tunnel.diagnose walks interface -> handshake
    -> raw TCP -> DNS -> HTTPS and stops at the first failure, so the last
    line of the log IS the fault.
    """
    ok, log = wg_tunnel.diagnose(_row(db))

    # A leftover proxy silently beats the tunnel.
    #
    # Worth reporting here even though it belongs to the bot, not the tunnel:
    # every one of these fields makes the bot connect some OTHER way, so a
    # stale value from an earlier attempt turns a healthy tunnel into a bot
    # that still does not work - and the tunnel test would keep saying green.
    bot = db.get(models.BotSettings, 1)
    if bot:
        for field, label in (
            ("telegram_proxy_url", "پروکسی SOCKS/HTTP"),
            ("telegram_api_proxy_url", "آدرس جایگزین API تلگرام"),
        ):
            value = (getattr(bot, field, None) or "").strip()
            if value:
                log.append(
                    f"هشدار: در تنظیمات ربات هنوز «{label}» روی «{value}» تنظیم است. "
                    "ربات از آن مسیر می‌رود، نه از این تونل. اگر از تلاش‌های قبلی مانده، خالی‌اش کنید."
                )

    return {"ok": ok, "log": log, "detail": log[-1] if log else ""}


@router.post("/refresh-cidrs")
def refresh_cidrs(db: Session = Depends(get_db)):
    """Re-reads Telegram's published ranges and re-applies the routes.

    Worth a button of its own because the ranges do change, and when they
    do the symptom is a bot that stops working long after anyone last
    touched the configuration - the hardest kind of failure to connect back
    to its cause.
    """
    row = _row(db)
    note = ""
    try:
        cidrs = wg_tunnel.fetch_telegram_cidrs()
        note = f"{len(cidrs)} رنج از منبع رسمی تلگرام گرفته و اعمال شد"
    except Exception as exc:  # noqa: BLE001
        # Falls back to the embedded list instead of failing. The live list
        # lives on core.telegram.org, which sits behind the very block this
        # tunnel exists to cross - so before the tunnel carries traffic this
        # fetch cannot succeed, and refusing to do anything would leave a
        # tunnel routing an incomplete set of ranges with no way to fix it
        # from the panel. The embedded list is the same published one.
        logger.info("دریافت زنده‌ی رنج‌ها ناموفق بود، فهرست داخلی اعمال شد: %s", exc)
        cidrs = list(wg_tunnel.DEFAULT_CIDRS)
        note = (f"دسترسی به core.telegram.org برقرار نشد، پس فهرست رسمی داخل پنل "
                f"({len(cidrs)} رنج) اعمال شد. این فهرست کامل است و تونل با آن درست کار می‌کند.")

    row.allowed_ips = ",".join(cidrs)
    row.cidrs_updated_at = dt.datetime.utcnow()
    db.commit()

    if row.enabled:
        try:
            wg_tunnel.up(row)
        except wg_tunnel.TunnelError as exc:
            row.last_error = f"{exc.message} | {exc.detail}"[:2000]
            db.commit()
            raise HTTPException(400, f"رنج‌ها ذخیره شد ولی اعمالشان ناموفق بود:\n{exc.message}")
    db.refresh(row)
    # `log` rather than a bare model: the card renders a log array, and this
    # is the one endpoint whose interesting output is WHICH list got applied
    # and why - information the model has no field for.
    return {**_out(db, row).model_dump(), "log": [note]}
