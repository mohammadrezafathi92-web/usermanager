"""«راه‌اندازی خودکار پروکسی تلگرام» - turn one of the panel's own MikroTik
nodes into the SOCKS5 proxy the Telegram bot dials through.

Why this exists as a panel feature rather than a page of commands to paste:
the panel already holds every MikroTik node's address and API credentials,
so the alternative is asking the owner to re-type by hand something the
panel could do exactly, every time, with the two settings that are easy to
get wrong (the idle timeout and the access lock) already correct.

The order below is deliberate and is the whole value of the endpoint:

  1. ASK THE ROUTER whether it can reach api.telegram.org, and stop if it
     cannot. A SOCKS proxy on a router that is itself blocked converts a
     clear connection error into a silent timeout - strictly worse than
     not having it. This is the single most common way this plan fails,
     because a router inside the same filtered network looks fine in every
     other respect.
  2. Configure and lock the proxy (see MikrotikClient.setup_socks_proxy).
  3. Only then hand the URL back for the admin to save.

Deliberately NOT done here: writing the URL into BotSettings and restarting
the bot. Configuring a proxy and switching the live bot onto it are two
decisions, and merging them would mean a half-working router could take the
bot down with no way back except SSH.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models
from ..config import settings
from ..database import get_db
from ..deps import require_superadmin
from ..services.mikrotik_client import MikrotikClient, MikrotikError

router = APIRouter(prefix="/api/telegram-proxy", tags=["telegram-proxy"],
                   dependencies=[Depends(require_superadmin)])

TELEGRAM_PROBE_URL = "https://api.telegram.org/"


class SetupIn(BaseModel):
    node_id: int
    port: int = 1080
    # The address the router will accept SOCKS connections FROM. Defaults to
    # the panel's own public host, which is what it is in every normal
    # install; overridable because a panel behind NAT egresses from a
    # different address than the one it is reached on, and only the operator
    # knows which.
    allowed_ip: Optional[str] = None
    idle_timeout: str = "10m"


class ProxyNodeOut(BaseModel):
    id: int
    name: str
    host: Optional[str]


def _mikrotik_node(db: Session, node_id: int) -> models.Node:
    node = db.get(models.Node, node_id)
    if node is None:
        raise HTTPException(404, "نود پیدا نشد")
    if not (node.mt_host or "").strip():
        raise HTTPException(400, "این نود آدرس میکروتیک ندارد - فقط روی نودهای میکروتیک می‌شود پروکسی ساخت")
    return node


def _allowed_ip(payload_ip: Optional[str]) -> str:
    value = (payload_ip or settings.panel_public_host or "").strip()
    if not value:
        raise HTTPException(
            400,
            "آدرس عمومی این سرور مشخص نیست. بدون آن نمی‌شود پروکسی را قفل کرد، و یک "
            "پروکسی باز روی اینترنت ظرف چند ساعت پیدا و سوءاستفاده می‌شود. "
            "آدرس را در تنظیمات سرور وارد کنید یا همین‌جا دستی بنویسید.",
        )
    # RouterOS wants a prefix; a bare address means "this one host".
    return value if "/" in value else f"{value}/32"


@router.get("/nodes", response_model=list[ProxyNodeOut])
def list_candidate_nodes(db: Session = Depends(get_db)):
    """MikroTik nodes this panel could turn into a proxy."""
    rows = (
        db.query(models.Node)
        .filter(models.Node.mt_host.isnot(None), models.Node.mt_host != "")
        .order_by(models.Node.name)
        .all()
    )
    return [ProxyNodeOut(id=n.id, name=n.name, host=n.mt_host) for n in rows]


@router.post("/check/{node_id}")
def check_node(node_id: int, db: Session = Depends(get_db)):
    """Can this router reach Telegram? Answered on its own so the admin can
    test a candidate before changing anything on it."""
    node = _mikrotik_node(db, node_id)
    try:
        with MikrotikClient.for_node(node) as client:
            ok, detail = client.check_reaches(TELEGRAM_PROBE_URL)
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001 - connection/credential problems
        raise HTTPException(400, f"اتصال به میکروتیک «{node.name}» ناموفق بود: {exc}")

    return {
        "ok": ok,
        "node": node.name,
        "detail": detail if ok else
        f"این میکروتیک خودش به api.telegram.org نمی‌رسد ({detail}). "
        "پروکسی روی آن هیچ مشکلی را حل نمی‌کند - نودی لازم است که خودش دسترسی داشته باشد.",
    }


@router.post("/setup")
def setup_proxy(payload: SetupIn, db: Session = Depends(get_db)):
    node = _mikrotik_node(db, payload.node_id)
    allowed = _allowed_ip(payload.allowed_ip)

    try:
        with MikrotikClient.for_node(node) as client:
            reachable, detail = client.check_reaches(TELEGRAM_PROBE_URL)
            if not reachable:
                # Refused rather than warned. Configuring it anyway would
                # leave an open port on a router that cannot serve its one
                # purpose, and the admin would discover it only when the
                # bot went quiet.
                raise HTTPException(
                    400,
                    f"«{node.name}» خودش به api.telegram.org دسترسی ندارد ({detail}). "
                    "روی چنین نودی پروکسی ساخته نشد، چون فقط خطای اتصال را به یک "
                    "تایم‌اوت بی‌صدا تبدیل می‌کرد.",
                )
            log = client.setup_socks_proxy(
                allowed_ip=allowed, port=payload.port, idle_timeout=payload.idle_timeout,
            )
    except HTTPException:
        raise
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"اتصال به میکروتیک «{node.name}» ناموفق بود: {exc}")

    return {
        "proxy_url": f"socks5://{node.mt_host}:{payload.port}",
        "node": node.name,
        "allowed_ip": allowed,
        "log": log,
        "next_step": "این آدرس را در فیلد پروکسی SOCKS ذخیره کنید و فیلد «آدرس پراکسی تلگرام» را خالی کنید - "
                     "این دو با هم جمع می‌شوند و اگر هر دو پر بمانند، ربات از این پروکسی تونل می‌زند تا برسد "
                     "به همان آدرس قبلی.",
    }


@router.post("/disable/{node_id}")
def disable_proxy(node_id: int, db: Session = Depends(get_db)):
    node = _mikrotik_node(db, node_id)
    try:
        with MikrotikClient.for_node(node) as client:
            log = client.disable_socks_proxy()
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"اتصال به میکروتیک «{node.name}» ناموفق بود: {exc}")
    return {"node": node.name, "log": log}
