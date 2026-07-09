from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import get_current_admin, require_permission
from ..services.mikrotik_client import MikrotikClient, MikrotikError
from ..services.xray_client import XrayError, client_for_node
from ..services import user_ops

# Router-level dependency is just "logged in" - listing/reading nodes is
# available to every admin regardless of permissions (a restricted admin
# still needs to see which servers exist to provision a connection for
# their own users - see routers/users.py's connection endpoints). Only the
# mutating endpoints below (create/update/delete/test/import/RADIUS push)
# additionally require the "manage_nodes" permission.
router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(get_current_admin)])
_manage = Depends(require_permission("manage_nodes"))


@router.get("", response_model=list[schemas.NodeOut])
def list_nodes(db: Session = Depends(get_db)):
    return db.query(models.Node).all()


@router.post("", response_model=schemas.NodeOut)
def create_node(payload: schemas.NodeCreate, db: Session = Depends(get_db), _perm=_manage):
    node = models.Node(**payload.model_dump())
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.get("/{node_id}", response_model=schemas.NodeOut)
def get_node(node_id: int, db: Session = Depends(get_db)):
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    return node


@router.put("/{node_id}", response_model=schemas.NodeOut)
def update_node(node_id: int, payload: schemas.NodeUpdate, db: Session = Depends(get_db), _perm=_manage):
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(node, k, v)
    db.commit()
    db.refresh(node)
    return node


@router.delete("/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db), _perm=_manage):
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    if node.connections:
        raise HTTPException(400, "ابتدا کانکشن‌های متصل به این نود را حذف کنید")
    db.delete(node)
    db.commit()
    return {"ok": True}


@router.post("/{node_id}/test")
def test_node(node_id: int, db: Session = Depends(get_db), _perm=_manage):
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    try:
        if node.type == models.NodeType.mikrotik:
            with MikrotikClient.for_node(node) as mt:
                mt.list_peers()
        else:
            with client_for_node(node) as xc:
                xc.test_connection()
                # For 3X-UI nodes, sync the real host/port/network/security/
                # sni from the panel itself so generated vless:// links are
                # correct even if the admin never filled these in by hand
                # (they otherwise silently default to port 443 + tls, which
                # produces a broken link).
                if getattr(node, "xr_panel_mode", "ssh") == "3xui" and hasattr(xc, "get_link_settings"):
                    info = xc.get_link_settings()
                    if info:
                        if info.get("host"):
                            node.xr_public_host = info["host"]
                        if info.get("port"):
                            node.xr_public_port = info["port"]
                        node.xr_network = info.get("network") or node.xr_network
                        node.xr_security = info.get("security") or node.xr_security
                        if info.get("sni"):
                            node.xr_sni = info["sni"]
                        db.commit()
        return {"ok": True, "message": "اتصال با موفقیت برقرار شد"}
    except (MikrotikError, XrayError) as exc:
        raise HTTPException(400, str(exc))


@router.post("/{node_id}/push-radius-config", response_model=schemas.RadiusPushResult)
def push_radius_config(node_id: int, payload: schemas.RadiusPushRequest, db: Session = Depends(get_db), _perm=_manage):
    """One-click alternative to typing the RouterOS commands by hand: uses
    the panel's existing RouterOS API connection to this node to register
    the panel as a /radius client (service=ppp) and switch `ppp aaa` to use
    it. Does NOT touch anything else (IP pool, OpenVPN/L2TP server,
    certificates, IPsec) - those remain fully manual, as with everything
    else in the OpenVPN/L2TP flow."""
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "این عملیات فقط برای نود میکروتیک است")
    if not node.mt_radius_secret:
        raise HTTPException(400, "ابتدا مقدار RADIUS Secret این نود را وارد و ذخیره کنید")

    panel_host = (payload.panel_host or settings.panel_public_host or "").strip()
    if not panel_host:
        raise HTTPException(
            400,
            "آدرس (IP) این سرور یوزر منیجر که میکروتیک باید برای RADIUS بهش وصل شود را وارد کنید",
        )

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.push_radius_config(
                panel_host=panel_host,
                secret=node.mt_radius_secret,
                auth_port=settings.radius_auth_port,
                acct_port=settings.radius_acct_port,
                interim_update=payload.interim_update or "00:05:00",
            )
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    return {"ok": True, "message": "تنظیمات RADIUS با موفقیت روی میکروتیک اعمال شد"}


@router.post("/{node_id}/import-ppp-users", response_model=schemas.PppImportResult)
def import_ppp_users(node_id: int, db: Session = Depends(get_db), _perm=_manage):
    """Reads /ppp/secret directly from the router (read-only) and imports
    any OpenVPN/L2TP account not already known to the panel as a new
    User+Connection, copying the same username/password so RADIUS auth
    keeps working for them without touching anything on the router."""
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    return user_ops.import_ppp_secrets(db, node)


@router.post("/{node_id}/import-usermanager-users", response_model=schemas.PppImportResult)
def import_usermanager_users(node_id: int, db: Session = Depends(get_db), _perm=_manage):
    """Reads accounts from MikroTik's own built-in User Manager
    (/user-manager/...) - a separate, protocol-agnostic RADIUS user database
    with its own quotas/expiry - and imports any not already known to the
    panel. Read-only on the router side."""
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    return user_ops.import_usermanager_accounts(db, node)


@router.post("/{node_id}/import-3xui-clients", response_model=schemas.PppImportResult)
def import_3xui_clients(node_id: int, db: Session = Depends(get_db), _perm=_manage):
    """Reads clients that already exist on the 3X-UI panel's configured
    inbound (created there before this node was connected) and imports any
    not already known to the panel as a new User+Connection, preserving
    their uuid/email/flow. Read-only on the 3X-UI panel side."""
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    return user_ops.import_threexui_clients(db, node)
