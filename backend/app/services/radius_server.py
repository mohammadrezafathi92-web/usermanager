"""Real RADIUS server (RFC2865 auth + RFC2866 accounting) for OpenVPN/L2TP
PPP users on MikroTik routers.

The panel itself is the RADIUS server: each MikroTik node with a
`mt_radius_secret` set can be pointed at this panel via
`/radius add service=ppp address=<panel-ip> secret=<secret>
authentication-port=1812 accounting-port=1813` (or use the "push RADIUS
config" button in the panel, which runs that for you over the existing
RouterOS API connection) plus `/ppp aaa set use-radius=yes accounting=yes`.

Supported authentication methods: PAP and CHAP (the two RouterOS offers for
PPP secrets/RADIUS by default). MS-CHAPv2 is NOT implemented - if the PPP
profile/router is configured to require MS-CHAPv2 only, authentication will
fail. Make sure the router's PPP profile allows pap/chap.

This module intentionally does not touch anything about the OpenVPN/L2TP
server, IP pool, certificates or IPsec - it only authenticates/accounts
username+password logins already created in this panel's database.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import datetime as dt

from pyrad import packet
from pyrad.dictionary import Dictionary
from pyrad.server import Server, RemoteHost

from .. import models
from ..config import settings
from ..database import SessionLocal
from .quota_manager import _apply_delta, _enforce_user_limits
from . import mschapv2

logger = logging.getLogger("radius_server")

DICT_PATH = os.path.join(os.path.dirname(__file__), "..", "radius", "dictionary")

PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp)

# Shared-users (max simultaneous sessions) enforcement: a new connection
# attempt beyond the limit is simply rejected (no CoA/disconnect-oldest -
# that would need the router to accept incoming Disconnect-Requests, which
# isn't set up by default and couldn't be tested live here). Repeated
# over-limit attempts in a short window temporarily ban the credential
# entirely, as a basic anti-abuse measure against a shared/leaked account.
OVERLIMIT_ATTEMPTS_THRESHOLD = 5
OVERLIMIT_WINDOW_SECONDS = 60
BAN_DURATION_MINUTES = 3

# A session with no Interim-Update/Stop for this long is assumed dead (a
# Stop packet was lost - router reboot, network blip, etc.) and pruned so
# it doesn't permanently count against the concurrent-session limit.
STALE_SESSION_MINUTES = 15


def _to_str(value) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="ignore")
    return str(value) if value is not None else ""


def _send_reply(server: "Server", pkt, reply) -> None:
    """MikroTik (like most modern RADIUS clients) sends a
    Message-Authenticator attribute (RFC 2869) on requests and expects one
    back on the reply; a reply missing it is where "Bad Reply"/silently
    dropped replies on the router side usually come from even though the
    packet itself is otherwise valid. Mirror it back when the request had
    one."""
    try:
        if getattr(pkt, "message_authenticator", None):
            reply.add_message_authenticator()
    except Exception:
        logger.exception("failed to add Message-Authenticator to RADIUS reply")
    server.SendReplyPacket(pkt.fd, reply)


def _gigaword_total(pkt, octets_attr: str, gigawords_attr: str) -> int:
    try:
        octets = int(pkt[octets_attr][0]) if octets_attr in pkt else 0
    except Exception:
        octets = 0
    try:
        gigawords = int(pkt[gigawords_attr][0]) if gigawords_attr in pkt else 0
    except Exception:
        gigawords = 0
    return octets + gigawords * (2 ** 32)


class UserManagerRadiusServer(Server):
    def __init__(self):
        super().__init__(
            addresses=[settings.radius_bind_host],
            dict=Dictionary(DICT_PATH),
            authport=settings.radius_auth_port,
            acctport=settings.radius_acct_port,
        )
        self.hosts = {}
        self._overlimit_attempts: dict[int, list] = {}
        self.refresh_hosts()

    # -------------------------------------------------------------- setup
    def refresh_hosts(self):
        """Reloads the NAS(router) -> shared-secret map from the DB, so a
        newly saved mt_radius_secret takes effect without a restart."""
        db = SessionLocal()
        try:
            nodes = (
                db.query(models.Node)
                .filter(
                    models.Node.type == models.NodeType.mikrotik,
                    models.Node.mt_radius_secret.isnot(None),
                    models.Node.mt_radius_secret != "",
                    models.Node.mt_host.isnot(None),
                )
                .all()
            )
            new_hosts = {}
            for node in nodes:
                new_hosts[node.mt_host] = RemoteHost(
                    node.mt_host, node.mt_radius_secret.encode(), node.name or node.mt_host
                )
            self.hosts = new_hosts
        except Exception:
            logger.exception("failed to refresh RADIUS host/secret map")
        finally:
            db.close()

    def start_background_refresh(self):
        def _loop():
            self.refresh_hosts()
            threading.Timer(settings.radius_hosts_refresh_seconds, _loop).start()

        threading.Timer(settings.radius_hosts_refresh_seconds, _loop).start()

    def run_forever(self):
        try:
            self.Run()
        except Exception:
            logger.exception("RADIUS server crashed")

    # ----------------------------------------------------- concurrent limit
    def _record_overlimit_attempt(self, connection: models.Connection) -> bool:
        """Tracks over-the-limit connection attempts per connection in a
        small in-memory sliding window (the RADIUS server loop is
        single-threaded, so no locking is needed). Returns True if this
        attempt just triggered a new ban."""
        now_ts = time.time()
        attempts = self._overlimit_attempts.setdefault(connection.id, [])
        attempts.append(now_ts)
        cutoff = now_ts - OVERLIMIT_WINDOW_SECONDS
        attempts[:] = [t for t in attempts if t >= cutoff]
        if len(attempts) >= OVERLIMIT_ATTEMPTS_THRESHOLD:
            connection.banned_until = dt.datetime.utcnow() + dt.timedelta(minutes=BAN_DURATION_MINUTES)
            attempts.clear()
            return True
        return False

    # --------------------------------------------------------------- auth
    def _check_password(self, pkt, expected_password: str) -> bool:
        """PAP and CHAP only (MS-CHAPv2 is not implemented - see module
        docstring). Attribute codes 2 (User-Password) and 3 (CHAP-Password)
        are read with a *numeric* key on purpose: pyrad's string-keyed
        __getitem__ runs these through the generic "string" attribute
        decoder, which is wrong for values that are raw encrypted/hashed
        bytes rather than text. Numeric-key access returns the untouched
        raw bytes, which is what PwDecrypt()/VerifyChapPasswd() expect."""
        try:
            if 2 in pkt:  # User-Password present -> PAP
                return pkt.PwDecrypt(pkt[2][0]) == expected_password
            if 3 in pkt:  # CHAP-Password present -> CHAP
                return pkt.VerifyChapPasswd(expected_password)
        except Exception:
            logger.exception("RADIUS password check failed")
        return False

    def _check_mschapv2(self, pkt, reply, username: str, expected_password: str) -> bool:
        """MS-CHAPv2 (see services/mschapv2.py) - the auth method most
        native L2TP/SSTP/IKEv2 clients use (SSTP on Windows in particular
        ALWAYS uses it, no PAP/CHAP fallback exists in the OS), unlike the
        OpenVPN client this panel already worked with. Only reached when
        _check_password above found no plain User-Password/CHAP-Password
        attribute to check (a pure MS-CHAPv2 request has neither), so this
        can't affect already-working PAP/CHAP logins. On success, also
        attaches MS-CHAP2-Success + MS-MPPE-Send/Recv-Key to `reply` -
        MikroTik expects these alongside a bare Access-Accept when the
        session negotiated MS-CHAPv2."""
        try:
            # pyrad already splits incoming Vendor-Specific attributes into
            # (vendor_id, vendor_type) tuple keys during decode - see the
            # module docstring in mschapv2.py's "RADIUS VSA wiring" section
            # for why reading and writing VSAs use two different pyrad
            # mechanisms. Numeric/tuple keys bypass dictionary-based value
            # decoding same as the `pkt[2]`/`pkt[3]` PAP/CHAP access above,
            # returning untouched raw bytes.
            chal_list = pkt.get((mschapv2.MS_VENDOR_ID, mschapv2.MS_CHAP_CHALLENGE))
            resp_list = pkt.get((mschapv2.MS_VENDOR_ID, mschapv2.MS_CHAP2_RESPONSE))
            chal = bytes(chal_list[0]) if chal_list else None
            resp = bytes(resp_list[0]) if resp_list else None
            if not chal or len(chal) != 16 or not resp or len(resp) < 50:
                return False  # not an MS-CHAPv2 request at all

            ident = resp[0]
            peer_challenge = resp[2:18]
            nt_response = resp[26:50]
            expected_nt_response = mschapv2.generate_nt_response(chal, peer_challenge, username, expected_password)
            if expected_nt_response != nt_response:
                logger.info("MS-CHAPv2 NT-Response mismatch for user=%r", username)
                return False

            auth_response = mschapv2.generate_authenticator_response(
                expected_password, nt_response, peer_challenge, chal, username
            )
            success_value = bytes([ident]) + auth_response.encode("ascii")
            reply.AddAttribute("Vendor-Specific", mschapv2.build_vsa(mschapv2.MS_CHAP2_SUCCESS, success_value))

            secret = pkt.secret if isinstance(pkt.secret, bytes) else pkt.secret.encode()
            send_key, recv_key = mschapv2.get_send_recv_keys(expected_password, nt_response)
            reply.AddAttribute(
                "Vendor-Specific",
                mschapv2.build_vsa(mschapv2.MS_MPPE_SEND_KEY, mschapv2.encrypt_mppe_key(send_key, secret, pkt.authenticator)),
            )
            reply.AddAttribute(
                "Vendor-Specific",
                mschapv2.build_vsa(mschapv2.MS_MPPE_RECV_KEY, mschapv2.encrypt_mppe_key(recv_key, secret, pkt.authenticator)),
            )
            return True
        except Exception:
            logger.exception("MS-CHAPv2 check failed for user=%r", username)
            return False

    def HandleAuthPacket(self, pkt):
        db = SessionLocal()
        try:
            reply = self.CreateReplyPacket(pkt)
            username = _to_str(pkt.get("User-Name", [None])[0])
            conn = (
                db.query(models.Connection)
                .filter(
                    models.Connection.ppp_username == username,
                    models.Connection.type.in_(PPP_TYPES),
                )
                .first()
            )
            ok = False
            reason = "ok"
            if not conn:
                reason = "no such connection/username in DB"
            elif conn.banned_until and conn.banned_until > dt.datetime.utcnow():
                reason = f"banned until {conn.banned_until.isoformat()} (too many over-limit attempts)"
            elif not conn.enabled:
                reason = "connection disabled"
            else:
                user = conn.user
                quota_ok = not user.total_quota_bytes or user.used_bytes < user.total_quota_bytes
                expiry_ok = not user.expire_at or user.expire_at > dt.datetime.utcnow()
                status_ok = user.status == models.UserStatus.active
                if not status_ok:
                    reason = f"user status={user.status}"
                elif not quota_ok:
                    reason = "quota exceeded"
                elif not expiry_ok:
                    reason = "expired"
                else:
                    ok = self._check_password(pkt, conn.ppp_password)
                    if not ok:
                        ok = self._check_mschapv2(pkt, reply, username, conn.ppp_password)
                    if not ok:
                        reason = "wrong password"
                    else:
                        if user.max_concurrent_sessions:
                            # User-level cap: counts currently-active
                            # connections across ALL of this user's services
                            # combined (e.g. an OpenVPN server + a WireGuard
                            # peer + a VLESS account bundled from one
                            # package), not just PPP ones. PPP (openvpn/
                            # l2tp) sessions come from RadiusActiveSession
                            # (real-time, this same auth flow); xray/
                            # wireguard have no live push to the panel, so
                            # they're counted from Connection.online, last
                            # refreshed by the periodic poll (poll_xray_node
                            # / poll_mikrotik_node in quota_manager.py) -
                            # meaning a xray/wireguard connection opened
                            # since the last poll cycle may not be reflected
                            # yet. This can only ever REJECT a new PPP login
                            # attempt (the one live enforcement point this
                            # panel has); it can't kick an already-open
                            # xray/wireguard session in real time.
                            limit = user.max_concurrent_sessions
                            ppp_count = (
                                db.query(models.RadiusActiveSession)
                                .join(models.Connection, models.Connection.id == models.RadiusActiveSession.connection_id)
                                .filter(
                                    models.Connection.user_id == user.id,
                                    models.Connection.type.in_(PPP_TYPES),
                                )
                                .count()
                            )
                            other_online_count = (
                                db.query(models.Connection)
                                .filter(
                                    models.Connection.user_id == user.id,
                                    models.Connection.type.notin_(PPP_TYPES),
                                    models.Connection.online.is_(True),
                                )
                                .count()
                            )
                            active_count = ppp_count + other_online_count
                        else:
                            # Legacy behavior: each connection's own cap,
                            # checked independently.
                            limit = conn.max_concurrent_sessions or 0
                            active_count = (
                                db.query(models.RadiusActiveSession)
                                .filter(models.RadiusActiveSession.connection_id == conn.id)
                                .count()
                                if limit else 0
                            )
                        if limit and active_count >= limit:
                            ok = False
                            just_banned = self._record_overlimit_attempt(conn)
                            reason = f"concurrent-session limit reached ({active_count}/{limit})"
                            if just_banned:
                                reason += " -> banned for %d min after repeated attempts" % BAN_DURATION_MINUTES
                            # Persist this event so it's visible from the panel
                            # (لاگ محدودیت اتصال page + UserDetail) instead of
                            # only ever existing in the container's own stdout
                            # logs - see models.RadiusLimitEventLog's docstring.
                            db.add(
                                models.RadiusLimitEventLog(
                                    connection_id=conn.id,
                                    user_id=user.id,
                                    owner_admin_id=user.owner_admin_id,
                                    username=username,
                                    connection_type=conn.type.value if hasattr(conn.type, "value") else str(conn.type),
                                    event_type="ban" if just_banned else "reject",
                                    active_count=active_count,
                                    limit_value=limit,
                                    banned_until=conn.banned_until if just_banned else None,
                                )
                            )
                        if ok and user.expire_at is None and user.expire_days_after_first_use:
                            # This is the user's first-ever successful login
                            # and their plan is set to "count validity from
                            # first use" rather than a fixed date - activate
                            # it now.
                            user.expire_at = dt.datetime.utcnow() + dt.timedelta(days=user.expire_days_after_first_use)
                            user.expire_days_after_first_use = None
                            logger.info(
                                "RADIUS: activated first-use expiry for user=%r -> expire_at=%s",
                                user.username, user.expire_at.isoformat(),
                            )
            db.commit()  # persists banned_until / first-use expiry activation if set above
            reply.code = packet.AccessAccept if ok else packet.AccessReject
            logger.info(
                "RADIUS Access-Request user=%r from=%s -> %s (%s)",
                username, pkt.source[0] if getattr(pkt, "source", None) else "?",
                "Accept" if ok else "Reject", reason,
            )
            _send_reply(self, pkt, reply)
        except Exception:
            logger.exception("RADIUS auth handling failed")
        finally:
            db.close()

    # ------------------------------------------------ active session bookkeeping
    @staticmethod
    def _open_active_session(db, connection_id: int, session_id: str, nas_ip) -> None:
        existing = (
            db.query(models.RadiusActiveSession)
            .filter(
                models.RadiusActiveSession.connection_id == connection_id,
                models.RadiusActiveSession.session_id == session_id,
            )
            .first()
        )
        if existing:
            existing.last_seen_at = dt.datetime.utcnow()
            existing.nas_ip = _to_str(nas_ip) or existing.nas_ip
            return
        db.add(
            models.RadiusActiveSession(
                connection_id=connection_id,
                session_id=session_id,
                nas_ip=_to_str(nas_ip) or None,
            )
        )

    @staticmethod
    def _touch_active_session(db, connection_id: int, session_id: str, nas_ip) -> None:
        existing = (
            db.query(models.RadiusActiveSession)
            .filter(
                models.RadiusActiveSession.connection_id == connection_id,
                models.RadiusActiveSession.session_id == session_id,
            )
            .first()
        )
        if existing:
            existing.last_seen_at = dt.datetime.utcnow()
        else:
            # Missed the Start packet - create it now so the concurrent-limit
            # count stays accurate.
            db.add(
                models.RadiusActiveSession(
                    connection_id=connection_id,
                    session_id=session_id,
                    nas_ip=_to_str(nas_ip) or None,
                )
            )

    @staticmethod
    def _close_active_session(db, connection_id: int, session_id: str) -> None:
        db.query(models.RadiusActiveSession).filter(
            models.RadiusActiveSession.connection_id == connection_id,
            models.RadiusActiveSession.session_id == session_id,
        ).delete(synchronize_session=False)

    # ---------------------------------------------------------- accounting
    def HandleAcctPacket(self, pkt):
        db = SessionLocal()
        try:
            reply = self.CreateReplyPacket(pkt)
            username = _to_str(pkt.get("User-Name", [None])[0])
            session_id = _to_str(pkt.get("Acct-Session-Id", [None])[0])
            status_raw = pkt.get("Acct-Status-Type", [None])[0]
            status = _to_str(status_raw)

            conn = (
                db.query(models.Connection)
                .filter(
                    models.Connection.ppp_username == username,
                    models.Connection.type.in_(PPP_TYPES),
                )
                .first()
            )
            nas_ip = pkt.source[0] if getattr(pkt, "source", None) else None

            if conn:
                if status == "Start":
                    conn.radius_session_id = session_id
                    conn.last_rx_bytes = 0
                    conn.last_tx_bytes = 0
                    self._open_active_session(db, conn.id, session_id, nas_ip)
                elif status in ("Interim-Update", "Stop"):
                    if conn.radius_session_id != session_id:
                        # We missed the Start (e.g. server restarted) - treat
                        # this as a fresh baseline instead of double-counting.
                        conn.radius_session_id = session_id
                        conn.last_rx_bytes = 0
                        conn.last_tx_bytes = 0
                    in_octets = _gigaword_total(pkt, "Acct-Input-Octets", "Acct-Input-Gigawords")
                    out_octets = _gigaword_total(pkt, "Acct-Output-Octets", "Acct-Output-Gigawords")
                    _apply_delta(db, conn, in_octets, out_octets)
                    if status == "Stop":
                        conn.radius_session_id = None
                        self._close_active_session(db, conn.id, session_id)
                    else:
                        self._touch_active_session(db, conn.id, session_id, nas_ip)

                user = conn.user
                if user:
                    _enforce_user_limits(db, user)
                db.commit()
                logger.info("RADIUS Acct-Request user=%r status=%s session=%s", username, status, session_id)
            else:
                logger.info("RADIUS Acct-Request user=%r status=%s -> no matching connection in DB", username, status)

            _send_reply(self, pkt, reply)
        except Exception:
            logger.exception("RADIUS accounting handling failed")
            db.rollback()
        finally:
            db.close()


def cleanup_stale_radius_sessions(stale_after_minutes: int = STALE_SESSION_MINUTES) -> int:
    """Deletes RadiusActiveSession rows that haven't been refreshed in a
    while (a lost Stop packet - router reboot, network blip, etc. - would
    otherwise permanently count against a connection's concurrent-session
    limit). Meant to be called periodically from the scheduler. Returns the
    number of rows deleted."""
    db = SessionLocal()
    try:
        cutoff = dt.datetime.utcnow() - dt.timedelta(minutes=stale_after_minutes)
        deleted = (
            db.query(models.RadiusActiveSession)
            .filter(models.RadiusActiveSession.last_seen_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info("cleaned up %d stale RADIUS active session(s)", deleted)
        return deleted
    except Exception:
        logger.exception("failed to clean up stale RADIUS sessions")
        db.rollback()
        return 0
    finally:
        db.close()


_server_instance: UserManagerRadiusServer | None = None


def start_radius_server_in_background():
    """Starts the RADIUS auth+accounting server on a daemon thread. Safe to
    call once at FastAPI startup. Does nothing if RADIUS_ENABLED=false."""
    global _server_instance
    if not settings.radius_enabled:
        logger.info("RADIUS server disabled via RADIUS_ENABLED=false")
        return None
    if _server_instance is not None:
        return _server_instance
    try:
        srv = UserManagerRadiusServer()
    except Exception:
        logger.exception("failed to initialize RADIUS server - it will not start")
        return None
    _server_instance = srv
    threading.Thread(target=srv.run_forever, name="radius-server", daemon=True).start()
    srv.start_background_refresh()
    logger.info(
        "RADIUS server listening on %s auth=%s acct=%s",
        settings.radius_bind_host, settings.radius_auth_port, settings.radius_acct_port,
    )
    return srv
