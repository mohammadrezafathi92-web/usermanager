from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..config import settings
from ..database import get_db
from ..deps import get_current_admin, require_permission
from ..services.mikrotik_client import MikrotikClient, MikrotikError
from ..services.xray_client import XrayError, client_for_node
from ..services import user_ops, hierarchy
from ..services.keys import generate_password


def _get_scoped_node(db: Session, node_id: int, admin: models.AdminUser) -> models.Node:
    """Fetches a node AND checks it's in this admin's hierarchy scope (see
    services/hierarchy.py) - 404s (not 403) for an out-of-scope node, same
    as a genuinely missing one, so a restricted Admin/Seller can't probe
    which node ids exist elsewhere in the system."""
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    allowed = hierarchy.accessible_node_ids(db, admin)
    if allowed is not None and node.id not in allowed:
        raise HTTPException(404, "نود پیدا نشد")
    return node


def _resolve_panel_host(payload_host: Optional[str]) -> str:
    panel_host = (payload_host or settings.panel_public_host or "").strip()
    if not panel_host:
        raise HTTPException(
            400,
            "آدرس (IP) این سرور یوزر منیجر که میکروتیک باید بهش وصل شود را وارد کنید",
        )
    return panel_host

# Router-level dependency is just "logged in" - listing/reading nodes is
# available to every admin regardless of permissions (a restricted admin
# still needs to see which servers exist to provision a connection for
# their own users - see routers/users.py's connection endpoints), further
# narrowed to their own hierarchy.accessible_node_ids scope below. The
# mutating endpoints are split into "edit_nodes" (update/test/import/
# RADIUS+protocol pushes on a node already in scope) and "delete_nodes" -
# see permissions.py's docstring on why this used to be one broad
# "manage_nodes" and was split into granular per-action permissions.
#
# create_node: a superadmin can always create a node (owner_admin_id=NULL,
# infrastructure they then optionally GRANT to specific level-2 Admins via
# AdminNodeAccess - see routers/admins.py's set_admin_nodes). A level-2
# Admin can ALSO create a node now (owner_admin_id=their own id) - their
# OWN server, added with their own IP/SSH credentials, confirmed with the
# panel owner ("کامل - افزودن سرور با IP/SSH خودش"). A level-3 Seller can
# never create one - they only ever work through their parent Admin's
# already-built Packages.
# delete_node: a superadmin can delete any node; a level-2 Admin can only
# delete a node THEY OWN (Node.owner_admin_id == their id) - never a
# superadmin-granted one, since deleting shared infrastructure out from
# under other Admins/customers using it isn't something an Admin should be
# able to do unilaterally (see delete_node below).
router = APIRouter(prefix="/api/nodes", tags=["nodes"], dependencies=[Depends(get_current_admin)])
_edit = Depends(require_permission("edit_nodes"))
_delete = Depends(require_permission("delete_nodes"))
_manage = _edit  # legacy alias, in case any other module still imports it


@router.get("", response_model=list[schemas.NodeOut])
def list_nodes(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    allowed = hierarchy.accessible_node_ids(db, admin)
    q = db.query(models.Node)
    if allowed is not None:
        q = q.filter(models.Node.id.in_(allowed)) if allowed else q.filter(False)
    return q.all()


@router.post("", response_model=schemas.NodeOut)
def create_node(payload: schemas.NodeCreate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    if hierarchy.is_seller(admin):
        raise HTTPException(403, "فروشنده‌ها اجازه ساخت سرور را ندارند")
    data = payload.model_dump()
    # owner_admin_id is always derived from who's creating it, never taken
    # from the payload - a superadmin's nodes stay global (NULL, still
    # grantable to any Admin via AdminNodeAccess), a level-2 Admin's own
    # node is scoped to themself (see hierarchy.accessible_node_ids).
    data["owner_admin_id"] = None if admin.is_superadmin else admin.id
    node = models.Node(**data)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


@router.get("/{node_id}", response_model=schemas.NodeOut)
def get_node(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    return _get_scoped_node(db, node_id, admin)


@router.put("/{node_id}", response_model=schemas.NodeOut)
def update_node(node_id: int, payload: schemas.NodeUpdate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    node = _get_scoped_node(db, node_id, admin)
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(node, k, v)
    db.commit()
    db.refresh(node)
    return node


@router.delete("/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    node = db.get(models.Node, node_id)
    if not node:
        raise HTTPException(404, "نود پیدا نشد")
    if not admin.is_superadmin and node.owner_admin_id != admin.id:
        # Covers both "out of scope entirely" (never even granted this
        # node) and "granted but not owned" (a superadmin-owned node this
        # Admin can USE, per AdminNodeAccess, but never delete) - same
        # error either way, no need to distinguish for the caller.
        raise HTTPException(403, "فقط سازنده این سرور یا ادمین اصلی می‌تواند آن را حذف کند")
    if node.connections:
        raise HTTPException(400, "ابتدا کانکشن‌های متصل به این نود را حذف کنید")
    db.delete(node)
    db.commit()
    return {"ok": True}


@router.post("/{node_id}/test")
def test_node(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    node = _get_scoped_node(db, node_id, admin)
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
def push_radius_config(node_id: int, payload: schemas.RadiusPushRequest, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """One-click alternative to typing the RouterOS commands by hand: uses
    the panel's existing RouterOS API connection to this node to register
    the panel as a /radius client (service=ppp) and switch `ppp aaa` to use
    it. Does NOT touch anything else (IP pool, OpenVPN/L2TP server,
    certificates, IPsec) - those remain fully manual, as with everything
    else in the OpenVPN/L2TP flow."""
    node = _get_scoped_node(db, node_id, admin)
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


@router.post("/{node_id}/push-sstp-config", response_model=schemas.ProtocolPushResult)
def push_sstp_config(node_id: int, payload: schemas.ProtocolPushRequest, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """One-click SSTP setup: registers the panel as a /radius client
    (service=ppp, same as push-radius-config) if not already done, creates+
    self-signs a server certificate if none exists yet, and enables the
    SSTP server with authentication=mschap2. Does NOT touch IP pools or PPP
    profiles - same minimal-touch scope as push-radius-config."""
    node = _get_scoped_node(db, node_id, admin)
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "این عملیات فقط برای نود میکروتیک است")
    if not node.mt_radius_secret:
        raise HTTPException(400, "ابتدا مقدار RADIUS Secret این نود را وارد و ذخیره کنید")

    panel_host = _resolve_panel_host(payload.panel_host)
    port = node.mt_sstp_port or 443

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.push_radius_config(
                panel_host=panel_host,
                secret=node.mt_radius_secret,
                auth_port=settings.radius_auth_port,
                acct_port=settings.radius_acct_port,
                service="ppp",
            )
            cert = mt.push_sstp_config(port=port)
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    node.mt_sstp_certificate = cert
    db.commit()
    return {"ok": True, "message": f"SSTP روی پورت {port} با گواهی «{cert}» فعال شد"}


@router.post("/{node_id}/push-l2tp-config", response_model=schemas.ProtocolPushResult)
def push_l2tp_config(node_id: int, payload: schemas.ProtocolPushRequest, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """One-click L2TP/IPsec setup: registers the panel as a /radius client
    (service=ppp) and enables the L2TP server with use-ipsec + a shared
    pre-shared key. Generates and saves a random IPsec secret onto this
    node if one isn't already set (mt_l2tp_ipsec_secret), so repeat pushes
    are idempotent and the same key can be shown to clients afterwards."""
    node = _get_scoped_node(db, node_id, admin)
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "این عملیات فقط برای نود میکروتیک است")
    if not node.mt_radius_secret:
        raise HTTPException(400, "ابتدا مقدار RADIUS Secret این نود را وارد و ذخیره کنید")

    panel_host = _resolve_panel_host(payload.panel_host)
    if not node.mt_l2tp_ipsec_secret:
        node.mt_l2tp_ipsec_secret = generate_password(20)
        db.commit()

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.push_radius_config(
                panel_host=panel_host,
                secret=node.mt_radius_secret,
                auth_port=settings.radius_auth_port,
                acct_port=settings.radius_acct_port,
                service="ppp",
            )
            mt.push_l2tp_config(ipsec_secret=node.mt_l2tp_ipsec_secret)
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    return {"ok": True, "message": f"L2TP/IPsec فعال شد. کلید IPsec: {node.mt_l2tp_ipsec_secret}"}


@router.post("/{node_id}/push-ikev2-config", response_model=schemas.ProtocolPushResult)
def push_ikev2_config(node_id: int, payload: schemas.ProtocolPushRequest, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """One-click IKEv2 setup: registers the panel as a /radius client for
    BOTH service=ppp (per-user login) and service=ipsec (IKEv2's own
    RADIUS/EAP relay), then sets up an /ip/ipsec peer+identity pinned to
    exchange-mode=ike2 with a pre-shared key (auto-generated and saved onto
    this node's mt_ikev2_psk if not already set) and enables the L2TP
    server's IPsec layer. NOTE: this configures the IPsec layer differently
    from push-l2tp-config (explicit ike2 peer vs. the server's own
    ipsec-secret shortcut) - pushing both on the same router may conflict;
    pick one PSK-based protocol per router unless you know what you're
    combining."""
    node = _get_scoped_node(db, node_id, admin)
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "این عملیات فقط برای نود میکروتیک است")
    if not node.mt_radius_secret:
        raise HTTPException(400, "ابتدا مقدار RADIUS Secret این نود را وارد و ذخیره کنید")

    panel_host = _resolve_panel_host(payload.panel_host)
    if not node.mt_ikev2_psk:
        node.mt_ikev2_psk = generate_password(20)
        db.commit()

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.push_radius_config(
                panel_host=panel_host,
                secret=node.mt_radius_secret,
                auth_port=settings.radius_auth_port,
                acct_port=settings.radius_acct_port,
                service="ppp",
            )
            mt.push_radius_config(
                panel_host=panel_host,
                secret=node.mt_radius_secret,
                auth_port=settings.radius_auth_port,
                acct_port=settings.radius_acct_port,
                service="ipsec",
            )
            mt.push_ikev2_config(psk=node.mt_ikev2_psk)
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    return {"ok": True, "message": f"IKEv2 فعال شد. کلید PSK: {node.mt_ikev2_psk}"}


@router.post("/{node_id}/import-ppp-users", response_model=schemas.PppImportResult)
def import_ppp_users(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """Reads /ppp/secret directly from the router (read-only) and imports
    any OpenVPN/L2TP account not already known to the panel as a new
    User+Connection, copying the same username/password so RADIUS auth
    keeps working for them without touching anything on the router."""
    node = _get_scoped_node(db, node_id, admin)
    return user_ops.import_ppp_secrets(db, node)


@router.post("/{node_id}/import-usermanager-users", response_model=schemas.PppImportResult)
def import_usermanager_users(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """Reads accounts from MikroTik's own built-in User Manager
    (/user-manager/...) - a separate, protocol-agnostic RADIUS user database
    with its own quotas/expiry - and imports any not already known to the
    panel. Read-only on the router side."""
    node = _get_scoped_node(db, node_id, admin)
    return user_ops.import_usermanager_accounts(db, node, admin)


@router.post("/{node_id}/import-3xui-clients", response_model=schemas.PppImportResult)
def import_3xui_clients(node_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """Reads clients that already exist on the 3X-UI panel's configured
    inbound (created there before this node was connected) and imports any
    not already known to the panel as a new User+Connection, preserving
    their uuid/email/flow. Read-only on the 3X-UI panel side."""
    node = _get_scoped_node(db, node_id, admin)
    return user_ops.import_threexui_clients(db, node)
