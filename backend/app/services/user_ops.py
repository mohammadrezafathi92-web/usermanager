"""Shared business logic for creating/renewing/removing users & connections.

Used by both the admin-panel router (JWT auth, browser) and the external
bot router (API-key auth) so behaviour stays identical between the two."""
from __future__ import annotations

import datetime as dt
import ipaddress
import re
import uuid
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from .mikrotik_client import MikrotikClient, MikrotikError
from .xray_client import XrayError, client_for_node
from .keys import generate_wireguard_keypair, generate_password
from .link_builder import (
    build_wireguard_config,
    build_vless_link,
    build_openvpn_config,
    build_l2tp_info,
    build_ikev2_info,
)

def gb_to_bytes(gb: float) -> int:
    return int(round((gb or 0) * 1024 ** 3))


# --------------------------------------------------------------------- users
def create_user_record(
    db: Session,
    username: str,
    full_name: Optional[str] = None,
    quota_gb: float = 0,
    expire_days: Optional[int] = None,
    notes: Optional[str] = None,
    telegram_id: Optional[int] = None,
    owner_admin_id: Optional[int] = None,
) -> models.User:
    if db.query(models.User).filter(models.User.username == username).first():
        raise HTTPException(400, "این نام کاربری قبلا ثبت شده است")
    if telegram_id and db.query(models.User).filter(models.User.telegram_id == telegram_id).first():
        raise HTTPException(400, "این حساب تلگرام قبلا به یک کاربر دیگر وصل شده است")
    expire_at = None
    if expire_days:
        expire_at = dt.datetime.utcnow() + dt.timedelta(days=expire_days)
    user = models.User(
        username=username,
        full_name=full_name,
        notes=notes,
        total_quota_bytes=gb_to_bytes(quota_gb),
        expire_at=expire_at,
        telegram_id=telegram_id,
        owner_admin_id=owner_admin_id,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def renew_user(
    db: Session,
    user: models.User,
    add_gb: float = 0,
    add_days: int = 0,
    reset_usage: bool = False,
) -> models.User:
    if add_gb:
        user.total_quota_bytes = (user.total_quota_bytes or 0) + gb_to_bytes(add_gb)
    if add_days:
        base = user.expire_at if (user.expire_at and user.expire_at > dt.datetime.utcnow()) else dt.datetime.utcnow()
        user.expire_at = base + dt.timedelta(days=add_days)
    if reset_usage:
        user.used_bytes = 0
    if user.status in (models.UserStatus.quota_exceeded, models.UserStatus.expired):
        user.status = models.UserStatus.active
    db.commit()
    db.refresh(user)
    return user


def delete_user_cascade(db: Session, user: models.User):
    for conn in list(user.connections):
        deprovision_connection(conn)
    db.delete(user)
    db.commit()


def bulk_delete_users(db: Session, user_ids: list[int], owner_admin_id: Optional[int] = None) -> dict:
    """owner_admin_id, when given (i.e. the caller is a non-superadmin - see
    routers/users.py), restricts this to only ids that admin actually owns -
    anything else is silently skipped, same as a plain missing id, so a
    non-superadmin can never delete another group's users by guessing ids."""
    deleted_count = 0
    for uid in user_ids:
        user = db.get(models.User, uid)
        if not user:
            continue
        if owner_admin_id is not None and user.owner_admin_id != owner_admin_id:
            continue
        delete_user_cascade(db, user)
        deleted_count += 1
    return {"deleted_count": deleted_count}


# ------------------------------------------------------------------- bulk ops
def bulk_create_users(
    db: Session,
    prefix: str,
    count: int,
    package_id: Optional[int] = None,
    quota_gb: float = 0,
    expire_days: Optional[int] = None,
    notes: Optional[str] = None,
    connections: Optional[list] = None,
    owner_admin_id: Optional[int] = None,
) -> dict:
    """Creates up to `count` users named prefix+1, prefix+2, ... prefix+N,
    each with the same quota/expiry, optionally provisioning the same set
    of connections (node+protocol) for every one of them. Numbers already
    taken (existing username) are skipped rather than overwritten, and
    numbering keeps going past them so you still end up with `count` new
    users when possible.

    If package_id is given, every user is built from that package instead
    (quota/duration/max-concurrent-sessions/services all come from the
    package, same as a single "ساخت با پکیج" - see routers/users.py's
    create_user) and quota_gb/expire_days/connections are ignored."""
    if count <= 0:
        raise HTTPException(400, "تعداد باید بزرگتر از صفر باشد")
    if count > 1000:
        raise HTTPException(400, "حداکثر ۱۰۰۰ کاربر در هر بار")

    package = None
    if package_id:
        package = db.get(models.Package, package_id)
        if not package:
            raise HTTPException(400, "پکیج پیدا نشد")

    created: list[str] = []
    skipped: list[dict] = []
    connections = connections or []

    i = 1
    attempts = 0
    max_attempts = count * 5 + 20  # safety cap in case of heavy collisions
    while len(created) < count and attempts < max_attempts:
        attempts += 1
        username = f"{prefix}{i}"
        i += 1
        if db.query(models.User).filter(models.User.username == username).first():
            skipped.append({"name": username, "reason": "این نام کاربری قبلا وجود دارد"})
            continue

        if package:
            user = create_user_record(db, username, notes=notes)
            user.total_quota_bytes = gb_to_bytes(package.quota_gb) if package.quota_gb else 0
            user.expire_at = (
                dt.datetime.utcnow() + dt.timedelta(days=package.duration_days) if package.duration_days else None
            )
            user.max_concurrent_sessions = package.max_concurrent_sessions
            user.owner_admin_id = owner_admin_id
            db.commit()
            db.refresh(user)
            result = provision_package_connections(db, user, package)
            for s in result["skipped"]:
                skipped.append({"name": f"{username} (اتصال)", "reason": s["reason"]})
        else:
            user = create_user_record(db, username, quota_gb=quota_gb, expire_days=expire_days, notes=notes)
            user.owner_admin_id = owner_admin_id
            db.commit()
            # Every service picked in this bulk-create form is one bundle
            # for this user, same idea as a package purchase - share one
            # batch so they group together in the bot's "اکانت من" screen.
            batch = uuid.uuid4().hex if connections else None
            for spec in connections:
                node = db.get(models.Node, spec.node_id)
                if not node:
                    skipped.append({"name": username, "reason": f"نود {spec.node_id} پیدا نشد (کاربر ساخته شد، اتصال ساخته نشد)"})
                    continue
                try:
                    provision_connection(
                        db, user, node, spec.protocol,
                        max_concurrent_sessions=getattr(spec, "max_concurrent_sessions", 1),
                        purchase_batch=batch,
                    )
                except HTTPException as exc:
                    skipped.append({"name": f"{username} (اتصال)", "reason": str(exc.detail)})
        created.append(username)

    return {
        "created": created,
        "created_count": len(created),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


def bulk_update_users(
    db: Session,
    user_ids: list,
    add_gb: float = 0,
    add_days: int = 0,
    reset_usage: bool = False,
    status: Optional[models.UserStatus] = None,
    max_concurrent_sessions: Optional[int] = None,
    owner_admin_id: Optional[int] = None,
) -> dict:
    """Applies the same renewal/status/limit change to every user in
    user_ids. Silently skips ids that don't exist - and, when
    owner_admin_id is given (non-superadmin caller), ids belonging to a
    different admin's group too."""
    updated = 0
    for uid in user_ids:
        user = db.get(models.User, uid)
        if not user:
            continue
        if owner_admin_id is not None and user.owner_admin_id != owner_admin_id:
            continue
        if add_gb or add_days or reset_usage:
            renew_user(db, user, add_gb=add_gb, add_days=add_days, reset_usage=reset_usage)
        if status is not None:
            user.status = status
        if max_concurrent_sessions is not None:
            # combined cap across all of the user's connections together -
            # see models.User.max_concurrent_sessions
            user.max_concurrent_sessions = max_concurrent_sessions
        updated += 1
    db.commit()
    return {"updated_count": updated}


# ------------------------------------------------------------------ wireguard
def _wg_gateway_and_client_ip(node: models.Node, db: Session) -> tuple[str, str]:
    subnet = ipaddress.ip_network(node.mt_client_subnet or "10.66.66.0/24")
    gateway_with_prefix = f"{subnet.network_address + 1}/{subnet.prefixlen}"

    used = set()
    for conn in db.query(models.Connection).filter(
        models.Connection.node_id == node.id,
        models.Connection.type == models.ConnectionType.wireguard,
    ):
        if conn.wg_client_address:
            used.add(conn.wg_client_address.split("/")[0])

    for host in subnet.hosts():
        if str(host) not in used and str(host) != str(subnet.network_address + 1):
            return gateway_with_prefix, f"{host}/32"
    raise HTTPException(400, "ظرفیت آدرس‌های WireGuard این نود تمام شده است")


def provision_wireguard(
    db: Session,
    user: models.User,
    node: models.Node,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    gateway_with_prefix, client_address = _wg_gateway_and_client_ip(node, db)
    private_key, public_key = generate_wireguard_keypair()
    peer_name = f"user-{user.username}-{uuid.uuid4().hex[:6]}"

    try:
        with MikrotikClient.for_node(node) as mt:
            mt.ensure_wireguard_interface(node.mt_wireguard_interface, node.mt_endpoint_port or 13231)
            mt.ensure_interface_address(node.mt_wireguard_interface, gateway_with_prefix)
            mt.add_peer(
                node.mt_wireguard_interface,
                public_key=public_key,
                allowed_address=client_address,
                comment=peer_name,
            )
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.wireguard,
        wg_peer_name=peer_name,
        wg_public_key=public_key,
        wg_private_key=private_key,
        wg_client_address=client_address,
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


# ------------------------------------------------------------- openvpn/l2tp
def _provision_ppp(
    db: Session,
    user: models.User,
    node: models.Node,
    service: str,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    """Creates only a username/password pair, stored in the panel's own
    database. Authentication now happens via RADIUS (this panel runs its own
    RADIUS server) instead of a local PPP secret on the router, so nothing
    needs to be pushed to the router here at all. The IP pool, the OpenVPN/
    L2TP server itself, certificates and IPsec are all expected to already
    be configured on the router by the admin - the panel does not touch any
    of that, by design. (The router only needs its /radius client entry
    pointed at this panel - see the "push RADIUS config" node action.)

    max_concurrent_sessions caps how many simultaneous RADIUS sessions this
    credential may have open at once (enforced by the RADIUS auth handler);
    0/None means unlimited."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    username = f"{user.username}-{service}-{uuid.uuid4().hex[:5]}"
    password = generate_password()

    conn_type = {
        "ovpn": models.ConnectionType.openvpn,
        "l2tp": models.ConnectionType.l2tp,
        "ikev2": models.ConnectionType.ikev2,
    }[service]
    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=conn_type,
        ppp_username=username,
        ppp_password=password,
        max_concurrent_sessions=max_concurrent_sessions if max_concurrent_sessions is not None else 1,
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def provision_openvpn(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "ovpn", max_concurrent_sessions, purchase_batch, package_name)


def provision_l2tp(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "l2tp", max_concurrent_sessions, purchase_batch, package_name)


def provision_ikev2(
    db: Session,
    user: models.User,
    node: models.Node,
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    return _provision_ppp(db, user, node, "ikev2", max_concurrent_sessions, purchase_batch, package_name)


# ---------------------------------------------------------------------- xray
def provision_xray(
    db: Session,
    user: models.User,
    node: models.Node,
    flow: str = "",
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    if node.type != models.NodeType.xray:
        raise HTTPException(400, "نود Xray معتبر نیست")

    email = f"{user.username}-{uuid.uuid4().hex[:6]}@usermanager.local"
    try:
        with client_for_node(node) as xc:
            client_uuid = xc.add_client(node.xr_inbound_tag, email, flow=flow or "")
    except XrayError as exc:
        raise HTTPException(400, str(exc))

    conn = models.Connection(
        user_id=user.id,
        node_id=node.id,
        type=models.ConnectionType.xray,
        xr_uuid=client_uuid,
        xr_email=email,
        xr_flow=flow or "",
        purchase_batch=purchase_batch,
        package_name_snapshot=package_name,
    )
    db.add(conn)
    db.commit()
    db.refresh(conn)
    return conn


def provision_package_connections(db: Session, user: models.User, package: models.Package) -> dict:
    """Provisions every server/service bundled into a package for this
    user in one go - used when a user is created "with a package" from the
    web panel (and the sales bot) instead of picking a node/protocol by
    hand. All connections created here share ONE auto-generated
    purchase_batch (see models.Connection.purchase_batch) and the package's
    current name as a display snapshot, so they show up as a single grouped
    "purchase" in the bot's "اکانت من" screen instead of a flat list."""
    created: list[models.Connection] = []
    skipped: list[dict] = []
    batch = uuid.uuid4().hex
    for pc in package.connections:
        node = db.get(models.Node, pc.node_id)
        if not node:
            skipped.append({"node_id": pc.node_id, "reason": "نود پیدا نشد"})
            continue
        try:
            # per-connection max_concurrent_sessions is just a fallback now
            # (see models.User.max_concurrent_sessions) - default to 1;
            # create_user_record/routers/users.py sets the real combined
            # cap from package.max_concurrent_sessions on the user itself.
            conn = provision_connection(
                db, user, node, pc.protocol, pc.flow or "", 1,
                purchase_batch=batch, package_name=package.name,
            )
            created.append(conn)
        except HTTPException as exc:
            skipped.append({"node_id": pc.node_id, "reason": str(exc.detail)})
    return {"created": created, "skipped": skipped}


def provision_connection(
    db: Session,
    user: models.User,
    node: models.Node,
    protocol: models.ConnectionType,
    flow: str = "",
    max_concurrent_sessions: Optional[int] = 1,
    purchase_batch: Optional[str] = None,
    package_name: Optional[str] = None,
) -> models.Connection:
    """Generic dispatcher used by the bot API, where the protocol is picked
    dynamically per request."""
    if protocol == models.ConnectionType.wireguard:
        return provision_wireguard(db, user, node, purchase_batch, package_name)
    if protocol == models.ConnectionType.openvpn:
        return provision_openvpn(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.l2tp:
        return provision_l2tp(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.ikev2:
        return provision_ikev2(db, user, node, max_concurrent_sessions, purchase_batch, package_name)
    if protocol == models.ConnectionType.xray:
        return provision_xray(db, user, node, flow, purchase_batch, package_name)
    raise HTTPException(400, "پروتکل نامعتبر است")


# ------------------------------------------------------------- deprovisioning
def deprovision_connection(connection: models.Connection):
    """Removes the connection from the remote node (MikroTik/Xray). Does
    NOT touch the database row - callers are expected to db.delete() after."""
    node = connection.node
    try:
        if connection.type == models.ConnectionType.wireguard:
            with MikrotikClient.for_node(node) as mt:
                peers = mt.list_peers(node.mt_wireguard_interface)
                match = next((p for p in peers if p.get("comment") == connection.wg_peer_name), None)
                if match:
                    mt.remove_peer(match[".id"])
        elif connection.type in (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2):
            # Authenticated via RADIUS against this panel's own database -
            # there is no remote PPP secret to remove, deleting the DB row
            # (done by the caller) is all that's needed.
            pass
        elif connection.type == models.ConnectionType.xray:
            with client_for_node(node) as xc:
                xc.remove_client(node.xr_inbound_tag, connection.xr_email, connection.xr_uuid)
    except (MikrotikError, XrayError) as exc:
        raise HTTPException(400, str(exc))


def delete_connection(db: Session, connection: models.Connection):
    deprovision_connection(connection)
    db.delete(connection)
    db.commit()


# -------------------------------------------------------- import PPP secrets
_PPP_SERVICE_TO_CONN_TYPE = {
    "ovpn": models.ConnectionType.openvpn,
    "l2tp": models.ConnectionType.l2tp,
}


def _secret_is_disabled(value) -> bool:
    return value in (True, "true", "yes")


def import_ppp_secrets(db: Session, node: models.Node) -> dict:
    """Reads /ppp/secret entries that were created directly on the router
    (outside the panel, before or alongside it) and creates matching
    User+Connection rows here so they show up in the panel and can be
    managed/quota-tracked going forward. Purely additive and read-only on
    the router side: nothing is changed or removed on the router, and any
    username that already exists as a panel connection is skipped rather
    than overwritten.

    Only 'ovpn' and 'l2tp' service secrets are imported (the only PPP
    services this panel understands); other services (pppoe, pptp, async,
    ...) are reported as skipped. Secrets with no password (e.g. already
    RADIUS-only) are skipped too since there's nothing to copy."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    try:
        with MikrotikClient.for_node(node) as mt:
            secrets_ = mt.read_ppp_secrets()
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    imported: list[str] = []
    skipped: list[dict] = []

    for secret in secrets_:
        name = secret.get("name")
        service = secret.get("service")
        password = secret.get("password")

        if not name:
            continue

        conn_type = _PPP_SERVICE_TO_CONN_TYPE.get(service)
        if conn_type is None:
            skipped.append({"name": name, "reason": f"سرویس پشتیبانی‌نشده ({service})"})
            continue

        if not password:
            skipped.append({"name": name, "reason": "این PPP secret پسورد ندارد (قابل کپی به RADIUS نیست)"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.ppp_username == name)
            .first()
        )
        if existing_conn:
            skipped.append({"name": name, "reason": "قبلا ایمپورت شده"})
            continue

        user = db.query(models.User).filter(models.User.username == name).first()
        if not user:
            user = models.User(
                username=name,
                notes="ایمپورت‌شده خودکار از PPP secret میکروتیک",
            )
            db.add(user)
            db.flush()

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=conn_type,
            enabled=not _secret_is_disabled(secret.get("disabled")),
            ppp_username=name,
            ppp_password=password,
            # /ppp/secret has no concept of a simultaneous-session limit -
            # default to unlimited rather than silently restricting an
            # already-working customer to a single connection.
            max_concurrent_sessions=0,
        )
        db.add(conn)
        imported.append(name)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# -------------------------------------------------- import User Manager accounts
_MT_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _parse_mikrotik_datetime(value) -> Optional[dt.datetime]:
    """RouterOS returns datetimes like 'jul/07/2026 10:00:00'."""
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    m = re.match(r"^([a-zA-Z]{3})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})$", value)
    if m:
        month = _MT_MONTHS.get(m.group(1).lower())
        if month:
            try:
                return dt.datetime(int(m.group(3)), month, int(m.group(2)), int(m.group(4)), int(m.group(5)), int(m.group(6)))
            except ValueError:
                return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_shared_users(value) -> int:
    """RouterOS User Manager's 'shared-users' is an integer or the literal
    string 'unlimited'. The panel represents unlimited as 0."""
    if value is None:
        return 1
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def import_usermanager_accounts(db: Session, node: models.Node) -> dict:
    """Reads accounts from MikroTik's own built-in User Manager
    (/user-manager/...), a separate RADIUS user database many admins already
    use - with its own quotas, expiry dates, and simultaneous-session
    limits - independently of /ppp/secret and independently of this panel.

    Unlike /ppp/secret, a User Manager account is NOT tied to a single
    service: the same username/password authenticates regardless of whether
    the client connects via OpenVPN, L2TP, PPPoE, etc., because User Manager
    itself has no "service" field. This creates one User + one Connection
    (stored as type=openvpn, but the same credentials also work for L2TP
    logins through this panel's RADIUS server, since its lookup doesn't
    discriminate by protocol either) per User Manager account:

    - total_quota_bytes is taken from the sum of download-limit/upload-limit
      (or transfer-limit if set) of all Limitations linked to the user's
      currently active/running Profile, via profile-limitation. 0 if none.
    - used_bytes is seeded from RouterOS's own persistent per-user lifetime
      counter, read via the "/user-manager/user monitor" command (NOT by
      summing /user-manager/session, which only retains a rolling window of
      recent sessions and badly undercounts anyone who has reconnected a
      few times - confirmed on a live router: monitor's total-download/
      total-upload matches exactly what Winbox's own User Manager > Users
      view shows, while summing /session came out ~100x too low). NOTE: if
      a user's real historical usage already exceeds the quota computed
      above, they will show as quota_exceeded (and get disabled) on the
      very next poll - check the numbers after import before relying on
      this for active customers.
    - expire_at is taken from the active Profile assignment's end-time
      (absent if the profile has no expiry / is not currently running).
    - max_concurrent_sessions is copied directly from the account's
      "shared-users" value (0 = unlimited), and is enforced going forward
      by this panel's own RADIUS server the same way User Manager enforced
      it (new connection attempts beyond the limit are rejected).

    Purely read-only on the router - nothing is changed there."""
    if node.type != models.NodeType.mikrotik:
        raise HTTPException(400, "نود میکروتیک معتبر نیست")

    try:
        with MikrotikClient.for_node(node) as mt:
            um_users = mt.read_um_users()
            user_profiles = mt.read_um_user_profiles()
            profile_limitations = mt.read_um_profile_limitations()
            limitations = mt.read_um_limitations()
            usage_by_id = mt.read_um_usage([u.get(".id") for u in um_users if u.get(".id")])
    except MikrotikError as exc:
        raise HTTPException(400, str(exc))

    # RouterOS sometimes returns these name/reference fields as ints (e.g. a
    # limitation literally named "100") instead of strings - normalize both
    # sides of every join to str() so a type mismatch never silently breaks
    # the lookup.
    limitation_by_name = {str(lim.get("name")): lim for lim in limitations if lim.get("name") is not None}

    # profile -> combined byte quota (sum of linked limitations; 0 = unlimited)
    profile_quota_bytes: dict[str, int] = {}
    for pl in profile_limitations:
        profile = pl.get("profile")
        lim = limitation_by_name.get(str(pl.get("limitation"))) if pl.get("limitation") is not None else None
        if not profile or not lim:
            continue
        try:
            transfer = int(lim.get("transfer-limit") or 0)
        except (TypeError, ValueError):
            transfer = 0
        if not transfer:
            try:
                transfer = int(lim.get("download-limit") or 0) + int(lim.get("upload-limit") or 0)
            except (TypeError, ValueError):
                transfer = 0
        if transfer:
            profile_quota_bytes[profile] = profile_quota_bytes.get(profile, 0) + transfer

    # user -> quota / expiry, from their currently active/running profile(s)
    # NOTE: RouterOS actually returns state as "running-active" (hyphenated),
    # not "running active" as MikroTik's own docs page shows it - confirmed
    # against a live router. Match any state starting with "running" (covers
    # "running-active" and any other "running-*" variant) rather than an
    # exact string, so this doesn't silently break again if a different
    # RouterOS version phrases it slightly differently.
    user_quota: dict[str, int] = {}
    user_expiry: dict[str, dt.datetime] = {}
    for up in user_profiles:
        state = (up.get("state") or "").strip().lower()
        if not state.startswith("running"):
            continue
        username = up.get("user")
        profile = up.get("profile")
        if not username:
            continue
        if profile in profile_quota_bytes:
            user_quota[username] = user_quota.get(username, 0) + profile_quota_bytes[profile]
        end_time = _parse_mikrotik_datetime(up.get("end-time"))
        if end_time and (username not in user_expiry or end_time > user_expiry[username]):
            user_expiry[username] = end_time

    # user -> true lifetime bytes used, from RouterOS's own per-user monitor
    # counter (keyed by the user's ".id", so join through um_users below).
    user_used: dict[str, int] = {}
    for um_user in um_users:
        uid = um_user.get(".id")
        name = um_user.get("name")
        if not uid or not name:
            continue
        row = usage_by_id.get(uid) or {}
        try:
            used = int(row.get("total-download") or 0) + int(row.get("total-upload") or 0)
        except (TypeError, ValueError):
            used = 0
        user_used[name] = used

    imported: list[str] = []
    skipped: list[dict] = []

    for um_user in um_users:
        name = um_user.get("name")
        password = um_user.get("password")
        if not name:
            continue
        if not password:
            skipped.append({"name": name, "reason": "این کاربر پسورد ندارد (شاید فقط OTP دارد)"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.ppp_username == name)
            .first()
        )
        if existing_conn:
            skipped.append({"name": name, "reason": "قبلا ایمپورت شده"})
            continue

        user = db.query(models.User).filter(models.User.username == name).first()
        if not user:
            user = models.User(
                username=name,
                notes="ایمپورت‌شده خودکار از User Manager میکروتیک",
                total_quota_bytes=user_quota.get(name, 0),
                used_bytes=user_used.get(name, 0),
                expire_at=user_expiry.get(name),
            )
            db.add(user)
            db.flush()

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=models.ConnectionType.openvpn,
            enabled=not _secret_is_disabled(um_user.get("disabled")),
            ppp_username=name,
            ppp_password=password,
            # Carries over RouterOS User Manager's "shared-users" (max
            # simultaneous sessions) as-is, so the limit that already
            # applied on the router keeps applying once the panel takes
            # over authentication.
            max_concurrent_sessions=_parse_shared_users(um_user.get("shared-users")),
        )
        db.add(conn)
        imported.append(name)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# -------------------------------------------------------- import 3X-UI clients
def import_threexui_clients(db: Session, node: models.Node) -> dict:
    """Reads clients that already exist directly on the 3X-UI panel's
    configured inbound (created there before this node was connected to the
    panel) and imports any not already known here as a new User+Connection,
    preserving their uuid/email/flow so the client's existing vless
    link/QR code keeps working unchanged. Their current up/down usage is
    seeded as a starting point on the shared quota. Quota/expiry
    enforcement moves to this panel going forward (same as every other
    import path here) rather than anything configured on 3X-UI itself.
    Purely read-only on the panel side - nothing is changed there."""
    if node.type != models.NodeType.xray or node.xr_panel_mode != "3xui":
        raise HTTPException(400, "این عملیات فقط برای نود Xray با روش اتصال «پنل 3X-UI» است")

    try:
        with client_for_node(node) as xc:
            clients = xc.list_clients_with_usage()
    except XrayError as exc:
        raise HTTPException(400, str(exc))

    imported: list[str] = []
    skipped: list[dict] = []

    for c in clients:
        email = c.get("email")
        client_uuid = c.get("id")
        if not email:
            continue
        if not client_uuid:
            skipped.append({"name": email, "reason": "این کلاینت شناسه (uuid) ندارد"})
            continue

        existing_conn = (
            db.query(models.Connection)
            .filter(models.Connection.xr_email == email, models.Connection.node_id == node.id)
            .first()
        )
        if existing_conn:
            skipped.append({"name": email, "reason": "قبلا ایمپورت شده"})
            continue

        # 3X-UI client emails are just a free-text label chosen in the panel
        # (not necessarily a real email) - use the part before "@" (if any)
        # as the panel username.
        username = (email.split("@")[0] or email).strip()
        if not username:
            skipped.append({"name": email, "reason": "نام کلاینت خالی است"})
            continue

        used_bytes = int(c.get("up", 0) or 0) + int(c.get("down", 0) or 0)
        total_quota_bytes = int(c.get("totalGB", 0) or 0)  # already raw bytes, see ThreeXUIClient
        expiry_ms = int(c.get("expiryTime", 0) or 0)
        expire_at = dt.datetime.utcfromtimestamp(expiry_ms / 1000) if expiry_ms > 0 else None
        # 3X-UI's "Start After First Use" client option is encoded as a
        # NEGATIVE expiryTime: -(days * 86400000) - it does NOT mean "no
        # expiry" (that's expiryTime == 0). Missing this meant every client
        # using that 3X-UI option imported as permanently unlimited instead
        # of carrying its day count over.
        expire_days_after_first_use = round(abs(expiry_ms) / 86400000) if expiry_ms < 0 else None

        user = db.query(models.User).filter(models.User.username == username).first()
        if not user:
            user = models.User(
                username=username,
                notes="ایمپورت‌شده خودکار از پنل 3X-UI",
                used_bytes=used_bytes,
                total_quota_bytes=total_quota_bytes,
                expire_at=expire_at,
                expire_days_after_first_use=expire_days_after_first_use,
            )
            db.add(user)
            db.flush()
        else:
            # merge this connection's history into the user's shared usage/
            # quota/expiry (take the larger quota, the later expiry - same
            # "don't silently shrink an existing entitlement" idea as the
            # MikroTik User Manager import).
            user.used_bytes = (user.used_bytes or 0) + used_bytes
            if total_quota_bytes and (not user.total_quota_bytes or total_quota_bytes > user.total_quota_bytes):
                user.total_quota_bytes = total_quota_bytes
            if expire_at and (not user.expire_at or expire_at > user.expire_at):
                user.expire_at = expire_at
                user.expire_days_after_first_use = None
            elif (
                expire_days_after_first_use
                and not user.expire_at
                and (not user.expire_days_after_first_use or expire_days_after_first_use > user.expire_days_after_first_use)
            ):
                user.expire_days_after_first_use = expire_days_after_first_use

        conn = models.Connection(
            user_id=user.id,
            node_id=node.id,
            type=models.ConnectionType.xray,
            enabled=bool(c.get("enable", True)),
            xr_uuid=client_uuid,
            xr_email=email,
            xr_flow=c.get("flow") or "",
            total_bytes=used_bytes,
        )
        db.add(conn)
        imported.append(email)

    db.commit()
    return {
        "imported": imported,
        "imported_count": len(imported),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


# ---------------------------------------------------------------- share info
def get_connection_share(connection: models.Connection) -> dict:
    """Returns {"kind": ..., "link": ..., "config_text": ...} for a
    connection, PLUS the individual fields that went into config_text
    (server/port/username/password/psk) so a caller that wants to render
    its own nicer, type-specific layout (e.g. the sales bot - see
    telegram_bot/connection_sender.py) doesn't have to re-parse the
    human-readable Persian config_text blob to get them back out."""
    node = connection.node
    if connection.type == models.ConnectionType.wireguard:
        try:
            with MikrotikClient.for_node(node) as mt:
                server_pub = mt.get_public_key(node.mt_wireguard_interface) or ""
        except MikrotikError as exc:
            raise HTTPException(400, str(exc))
        text = build_wireguard_config(connection, node, server_pub)
        return {
            "kind": "wireguard", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": node.mt_endpoint_port,
            "username": None, "password": None, "psk": None,
        }

    if connection.type == models.ConnectionType.openvpn:
        text = build_openvpn_config(connection, node)
        return {
            "kind": "openvpn", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": node.mt_ovpn_port or 1194,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": None,
        }

    if connection.type == models.ConnectionType.l2tp:
        text = build_l2tp_info(connection, node)
        psk = node.mt_l2tp_ipsec_secret if node.mt_l2tp_use_ipsec else None
        return {
            "kind": "l2tp", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": None,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": psk,
        }

    if connection.type == models.ConnectionType.ikev2:
        text = build_ikev2_info(connection, node)
        return {
            "kind": "ikev2", "link": None, "config_text": text,
            "server": node.mt_endpoint_host, "port": None,
            "username": connection.ppp_username, "password": connection.ppp_password, "psk": node.mt_ikev2_psk,
        }

    # xray
    link = build_vless_link(connection, node)
    return {
        "kind": "vless", "link": link, "config_text": None,
        "server": None, "port": None, "username": None, "password": None, "psk": None,
    }
