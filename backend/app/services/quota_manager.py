"""Background job: pull traffic counters from every node, add the deltas
onto each connection's owning user (shared quota across all protocols),
and enable/disable connections as quota / expiry dictate."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from .. import models
from ..database import SessionLocal
from .mikrotik_client import MikrotikClient, MikrotikError, parse_ros_duration_seconds
from .xray_client import XrayError, client_for_node

logger = logging.getLogger("quota_manager")

PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp)

# A WireGuard peer whose last cryptographic handshake was more recent than
# this counts as "currently connected" for online-status display and the
# cross-protocol concurrent-session count (see radius_server.py). Peers here
# use a 25s persistent-keepalive (see link_builder.py), and WireGuard itself
# re-handshakes at least every 120s of active traffic, so a live connection
# should never go this long between handshakes - this threshold just adds a
# safety margin over one missed poll/keepalive cycle.
WIREGUARD_ONLINE_THRESHOLD_SECONDS = 180


def _apply_delta(db: Session, connection: models.Connection, rx: int, tx: int):
    """Given fresh cumulative rx/tx from the node, compute the delta since
    last poll (handling counter resets) and add it to the user's usage."""
    prev_total = (connection.last_rx_bytes or 0) + (connection.last_tx_bytes or 0)
    new_total = (rx or 0) + (tx or 0)

    if new_total >= prev_total:
        delta = new_total - prev_total
    else:
        # counters were reset (peer recreated / xray restarted / ppp session
        # reconnected with a fresh dynamic interface)
        delta = new_total

    if delta <= 0:
        connection.last_rx_bytes = rx
        connection.last_tx_bytes = tx
        return

    connection.last_rx_bytes = rx
    connection.last_tx_bytes = tx
    connection.total_bytes = (connection.total_bytes or 0) + delta

    user: models.User = connection.user
    user.used_bytes = (user.used_bytes or 0) + delta

    db.add(models.UsageLog(user_id=user.id, connection_id=connection.id, delta_bytes=delta))

    # Usage-based reseller billing (see AdminUser.billing_mode) - for
    # admins in "usage" mode, this single choke point (every protocol's
    # traffic ends up here - WireGuard/Xray polling, and RADIUS accounting
    # for OpenVPN/L2TP/IKEv2) is where their GB volume pool depletes in
    # near-real-time, instead of being charged a flat price per package at
    # creation time (see routers/users.py's _charge_admin_for_package).
    admin = user.owner_admin
    if admin is not None and not admin.is_superadmin and admin.billing_mode == "usage":
        admin.volume_balance_gb = (admin.volume_balance_gb or 0) - (delta / (1024 ** 3))


def _enforce_user_limits(db: Session, user: models.User):
    exceeded = user.total_quota_bytes and user.used_bytes >= user.total_quota_bytes
    expired = user.expire_at and user.expire_at < dt.datetime.utcnow()

    if user.status == models.UserStatus.disabled:
        return  # manually disabled by admin - do not touch

    if exceeded:
        target_status = models.UserStatus.quota_exceeded
    elif expired:
        target_status = models.UserStatus.expired
    else:
        target_status = models.UserStatus.active

    if target_status == user.status:
        return

    user.status = target_status
    for conn in user.connections:
        _set_connection_enabled(db, conn, enabled=(target_status == models.UserStatus.active))


def _set_connection_enabled(db: Session, connection: models.Connection, enabled: bool):
    if connection.enabled == enabled:
        return
    node: models.Node = connection.node
    try:
        if connection.type == models.ConnectionType.wireguard:
            with MikrotikClient.for_node(node) as mt:
                peers = mt.list_peers(node.mt_wireguard_interface)
                match = next((p for p in peers if p.get("comment") == connection.wg_peer_name), None)
                if match:
                    mt.set_peer_disabled(match[".id"], disabled=not enabled)
        elif connection.type in PPP_TYPES:
            # Authenticated via RADIUS - the RADIUS auth handler checks
            # connection.enabled live on every Access-Request, so flipping
            # the DB flag is all that's needed to cut the user off (RouterOS
            # will simply reject the next re-auth / already-open PPP session
            # will be kicked on the router's own timeout or can be dropped
            # manually if instant cutoff is required).
            pass
        elif connection.type == models.ConnectionType.xray:
            with client_for_node(node) as xc:
                xc.set_client_enabled(
                    node.xr_inbound_tag, connection.xr_email, connection.xr_uuid,
                    connection.xr_flow or "", enabled,
                )
        connection.enabled = enabled
    except (MikrotikError, XrayError) as exc:
        logger.warning("failed to toggle connection %s: %s", connection.id, exc)


def poll_mikrotik_node(db: Session, node: models.Node):
    """Polls WireGuard peer counters. OpenVPN/L2TP (PPP) usage is no longer
    polled here - it now arrives in real time via RADIUS accounting packets
    (see services/radius_server.py), which call _apply_delta directly as
    Interim-Update/Stop packets come in.

    Even when a node has zero WireGuard peers (openvpn/l2tp-only nodes,
    which is now the common case), we still make a lightweight RouterOS API
    call here so last_seen/last_error - and therefore the "آنلاین" status
    shown on the dashboard - stay accurate. Previously this function
    returned immediately for WireGuard-less nodes without ever touching
    last_seen, so such nodes always showed as offline regardless of their
    real status."""
    wg_conns = [c for c in node.connections if c.type == models.ConnectionType.wireguard]
    try:
        with MikrotikClient.for_node(node) as mt:
            peers = mt.list_peers(node.mt_wireguard_interface if wg_conns else None)
            if wg_conns:
                by_comment = {p.get("comment"): p for p in peers if p.get("comment")}
                for conn in wg_conns:
                    peer = by_comment.get(conn.wg_peer_name)
                    if not peer:
                        conn.online = False
                        continue
                    rx = int(peer.get("rx", 0) or 0)
                    tx = int(peer.get("tx", 0) or 0)
                    _apply_delta(db, conn, rx, tx)
                    # RouterOS has no explicit online/offline flag for
                    # WireGuard - only a "how long since last handshake"
                    # duration, which we treat as "currently connected" if
                    # recent enough. Feeds both the آنلاین/آفلاین badge
                    # (UserDetail.jsx already renders it for every
                    # connection type) and the cross-protocol concurrent-
                    # session count in radius_server.py.
                    age = parse_ros_duration_seconds(peer.get("last-handshake"))
                    conn.online = age is not None and age <= WIREGUARD_ONLINE_THRESHOLD_SECONDS

        node.last_seen = dt.datetime.utcnow()
        node.last_error = None
    except MikrotikError as exc:
        node.last_error = str(exc)
        logger.warning("mikrotik node %s error: %s", node.id, exc)


def poll_xray_node(db: Session, node: models.Node):
    connections = [c for c in node.connections if c.type == models.ConnectionType.xray]
    if not connections:
        return
    try:
        with client_for_node(node) as xc:
            stats = xc.query_all_user_stats()
            for conn in connections:
                bucket = stats.get(conn.xr_email)
                if not bucket:
                    continue
                _apply_delta(db, conn, bucket.get("downlink", 0), bucket.get("uplink", 0))

            # Live online/offline flag (3X-UI only - see
            # ThreeXUIClient.get_online_emails; SSH-managed nodes always get
            # back an empty "unknown" set here, so their connections simply
            # stay marked offline, same as before this feature existed).
            online_emails = xc.get_online_emails()
            for conn in connections:
                conn.online = bool(conn.xr_email) and conn.xr_email in online_emails
                if conn.online:
                    # Mirrors radius_server.py's "count validity from first
                    # use" activation - PPP logins go through RADIUS, which
                    # has its own copy of this check, but xray/VLESS has no
                    # such login event, so a xray-only user's
                    # expire_days_after_first_use would otherwise never
                    # fire. Being seen online here is the xray equivalent of
                    # "first successful login". Idempotent: once activated,
                    # expire_days_after_first_use is cleared to None, so
                    # this is a no-op on every later poll.
                    user = conn.user
                    if user.expire_at is None and user.expire_days_after_first_use:
                        user.expire_at = dt.datetime.utcnow() + dt.timedelta(days=user.expire_days_after_first_use)
                        user.expire_days_after_first_use = None
                        logger.info(
                            "xray poll: activated first-use expiry for user=%r -> expire_at=%s",
                            user.username, user.expire_at.isoformat(),
                        )
        node.last_seen = dt.datetime.utcnow()
        node.last_error = None
    except XrayError as exc:
        node.last_error = str(exc)
        logger.warning("xray node %s error: %s", node.id, exc)


def poll_all():
    db = SessionLocal()
    try:
        nodes = db.query(models.Node).filter(models.Node.enabled == True).all()  # noqa: E712
        for node in nodes:
            if node.type == models.NodeType.mikrotik:
                poll_mikrotik_node(db, node)
            elif node.type == models.NodeType.xray:
                poll_xray_node(db, node)

        users = db.query(models.User).all()
        for user in users:
            _enforce_user_limits(db, user)

        db.commit()
    except Exception:
        logger.exception("poll_all failed")
        db.rollback()
    finally:
        db.close()
