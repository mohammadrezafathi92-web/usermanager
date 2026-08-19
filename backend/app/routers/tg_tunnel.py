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
from ..services import wg_tunnel
from ..services.mikrotik_client import MikrotikClient, MikrotikError

logger = logging.getLogger("tg_tunnel")

router = APIRouter(prefix="/api/telegram-tunnel", tags=["telegram-tunnel"],
                   dependencies=[Depends(require_superadmin)])

# The panel's address inside the tunnel. A /30 with the node on .1 and the
# panel on .2 mirrors how the node's own point-to-point links are already
# numbered, so an operator reading the router's config sees a familiar shape.
DEFAULT_SUBNET = "10.77.0.0/30"


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
    iface = (payload.wg_interface or node.mt_wireguard_interface or "wireguard1").strip()

    import ipaddress
    net = ipaddress.ip_network(DEFAULT_SUBNET)
    hosts = list(net.hosts())
    peer_addr = payload.peer_address or f"{hosts[0]}/{net.prefixlen}"
    own_addr = payload.address or f"{hosts[1]}/{net.prefixlen}"

    # A fresh keypair on every setup. Reusing the old one would silently
    # keep working against a peer entry that was meant to be replaced, and
    # the cost of rotating is nil.
    private_key, public_key = wg_tunnel.generate_keypair()

    log: list[str] = []
    try:
        with MikrotikClient.for_node(node) as client:
            client.ensure_wireguard_interface(iface)
            peer_public = client.get_public_key(iface)
            if not peer_public:
                raise HTTPException(
                    400,
                    f"اینترفیس «{iface}» روی نود کلید عمومی ندارد. یک‌بار در خود روتر بسازیدش و دوباره امتحان کنید.",
                )
            listen_port = payload.endpoint_port or node.mt_endpoint_port or 13231

            existing = [p for p in client.list_peers(iface) if (p.get("comment") or "") == _PEER_COMMENT]
            for p in existing:
                client.remove_peer(p[".id"])
            client.add_peer(
                interface=iface,
                public_key=public_key,
                allowed_address=own_addr.split("/")[0] + "/32",
                comment=_PEER_COMMENT,
            )
            log.append(f"پیر روی نود «{node.name}» ثبت شد (اینترفیس {iface})")
    except HTTPException:
        raise
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"اتصال به نود «{node.name}» ناموفق بود: {exc}")

    row.node_id = node.id
    row.interface_name = "wg-tg"
    row.private_key = private_key
    row.public_key = public_key
    row.address = own_addr
    row.peer_public_key = peer_public
    row.peer_endpoint = f"{node.mt_endpoint_host or node.mt_host}:{listen_port}"
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
    ok, detail = wg_tunnel.test_telegram()
    return {"ok": ok, "detail": detail}


@router.post("/refresh-cidrs", response_model=TunnelOut)
def refresh_cidrs(db: Session = Depends(get_db)):
    """Re-reads Telegram's published ranges and re-applies the routes.

    Worth a button of its own because the ranges do change, and when they
    do the symptom is a bot that stops working long after anyone last
    touched the configuration - the hardest kind of failure to connect back
    to its cause.
    """
    row = _row(db)
    try:
        cidrs = wg_tunnel.fetch_telegram_cidrs()
    except Exception as exc:  # noqa: BLE001
        # The list lives on core.telegram.org, which is behind the same block
        # the tunnel exists to bypass - so this button only works once the
        # tunnel is already carrying traffic. Said plainly, because the raw
        # connection error reads like the feature is broken when it is not.
        raise HTTPException(
            400,
            "فهرست رنج‌ها روی core.telegram.org است که خودش پشت همین فیلترینگ قرار دارد، "
            "پس این دکمه فقط وقتی کار می‌کند که تونل از قبل بالا و سالم باشد.\n\n"
            "تونل بدون این دکمه هم کامل کار می‌کند: فهرست رسمی رنج‌ها از قبل داخل پنل هست. "
            "این دکمه فقط برای وقتی است که تلگرام رنج‌هایش را عوض کند.\n\n"
            f"خطای اصلی: {exc}",
        )

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
    return _out(db, row)
