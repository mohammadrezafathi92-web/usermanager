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
from ..telegram_bot import runner as telegram_bot_runner
from .quota_manager import _apply_delta, _enforce_user_limits, _enforce_purchase_limits
from .user_ops import _maybe_activate_reserved_renewal, _maybe_activate_reserved_purchase_renewal
from . import mschapv2
from . import ip_guard

logger = logging.getLogger("radius_server")

DICT_PATH = os.path.join(os.path.dirname(__file__), "..", "radius", "dictionary")

PPP_TYPES = (models.ConnectionType.openvpn, models.ConnectionType.l2tp, models.ConnectionType.ikev2, models.ConnectionType.sstp)

# MikroTik's RADIUS vendor-specific attributes (RouterOS docs) - used to
# push Connection.speed_limit_mbps on every successful PPP auth. Same raw
# VSA wire format as the Microsoft ones in mschapv2.py (build_vsa is
# generic over vendor_id), just a different vendor/attribute number.
MIKROTIK_VENDOR_ID = 14988
MIKROTIK_RATE_LIMIT_ATTR = 8

# Shared-users (max simultaneous sessions) enforcement: a new connection
# attempt beyond the limit is simply rejected (no CoA/disconnect-oldest -
# that would need the router to accept incoming Disconnect-Requests, which
# isn't set up by default and couldn't be tested live here). Repeated
# over-limit attempts in a short window temporarily ban the credential
# entirely, as a basic anti-abuse measure against a shared/leaked account.
OVERLIMIT_ATTEMPTS_THRESHOLD = 5
OVERLIMIT_WINDOW_SECONDS = 60

# How long to wait before logging the SAME rejection reason for the same
# client again. A device with a saved wrong password retries indefinitely;
# without this the log becomes thousands of identical rows and the database
# grows without bound. Ten minutes keeps "this is still happening" visible
# while collapsing a retry storm into one line.
REJECTION_LOG_WINDOW_SECONDS = 600

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


def _ban_notification_text(username: str, minutes: int, banned_until: dt.datetime) -> str:
    return (
        f"🚫 حساب <b>{username}</b> شما به‌طور موقت مسدود شد.\n\n"
        f"دلیل: استفاده هم‌زمان بیش از حد مجاز (تعداد اتصال فعال بیشتر از سقفی که برای این اکانت "
        f"تعیین شده است) - معمولاً یعنی از این اشتراک روی دستگاه‌های بیشتری از حد مجاز استفاده "
        f"می‌شود.\n\n"
        f"مدت مسدودیت: {minutes} دقیقه (تا ساعت {banned_until.strftime('%H:%M')})\n\n"
        "پس از پایان این مدت اتصال به‌صورت خودکار دوباره ممکن می‌شود. اگر این اتفاق تکرار شود، "
        "لطفاً تعداد دستگاه‌های متصل را کاهش دهید یا برای افزایش سقف اتصال هم‌زمان با پشتیبانی در "
        "تماس باشید."
    )


def _notify_ban_async(telegram_id: int, username: str, minutes: int, banned_until: dt.datetime) -> None:
    """Fires the ban-notification Telegram message on its own background
    thread rather than calling telegram_bot_runner.send_message_sync
    directly here - that call is a blocking HTTP round-trip (up to 10s
    timeout), and this function is invoked from inside HandleAuthPacket,
    the live RADIUS auth path. Blocking that thread for a Telegram request
    would delay/risk-timeout the Access-Reject this packet is about to send
    back to the router, and this server processes RADIUS packets one at a
    time - so it would stall every OTHER connection's auth attempts too
    while it waits. Best-effort/fire-and-forget: failures (bot not
    configured, customer blocked the bot, etc.) are swallowed exactly like
    every other send_message_sync caller in this project (see
    services/notify.py)."""
    def _run():
        try:
            telegram_bot_runner.send_message_sync(telegram_id, _ban_notification_text(username, minutes, banned_until))
        except Exception:
            logger.exception("failed to send ban notification to telegram_id=%s", telegram_id)

    threading.Thread(target=_run, name="ban-notify", daemon=True).start()


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
        # (reason, username, client ip) -> when it was last written. See
        # _should_log_rejection.
        self._rejection_log_times: dict[str, float] = {}
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
    def _should_log_rejection(self, key: str) -> bool:
        """Rate-limits the rejection log to one row per reason per client per
        window.

        A phone with a saved wrong password retries every few seconds,
        forever. Writing a row each time would turn a useful history into
        thousands of identical lines and grow the database without bound -
        which this panel has already been through once (see
        quota_manager's USAGE_LOG_KEEP_DAYS, added after the disk filled).

        In-memory and unlocked, like _overlimit_attempts above: the RADIUS
        loop is single-threaded. Losing the window on restart just means one
        extra row per reason, which is the harmless direction.
        """
        now_ts = time.time()
        last = self._rejection_log_times.get(key)
        if last is not None and now_ts - last < REJECTION_LOG_WINDOW_SECONDS:
            return False
        # Bounded, so a flood of unknown usernames cannot grow this forever.
        if len(self._rejection_log_times) > 5000:
            self._rejection_log_times.clear()
        self._rejection_log_times[key] = now_ts
        return True

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
            # Best-effort caller IP for RadiusLimitEventLog below - prefer
            # Calling-Station-Id (the client's real remote IP for PPP-based
            # protocols, when the NAS sends it) and fall back to the NAS's
            # own source IP (the MikroTik router itself) if not - see
            # models.RadiusLimitEventLog.client_ip's docstring.
            client_ip = _to_str(pkt.get("Calling-Station-Id", [None])[0]) or (
                pkt.source[0] if getattr(pkt, "source", None) else None
            )
            # A client_ip already on services/ip_guard.py's block list (auto-
            # banned for repeated unknown-username/disabled-connection
            # attempts from here, or banned by hand from Settings) is
            # refused outright - no DB lookups, no password check, nothing
            # that could be timing-probed or that costs a query per packet.
            if client_ip and ip_guard.is_banned(client_ip):
                reply.code = packet.AccessReject
                _send_reply(self, pkt, reply)
                return
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
            # A STABLE key for the panel's event log, set wherever `reason`
            # is. Deliberately not derived from `reason`: that string is
            # prose meant for a human reading container logs, and matching
            # on it would mean rewording a message silently stops the
            # logging. None = do not log (either it succeeded, or the
            # branch writes its own richer row).
            reject_kind = None
            if not conn:
                reason = "no such connection/username in DB"
                reject_kind = "unknown_user"
            elif conn.banned_until and conn.banned_until > dt.datetime.utcnow():
                reason = f"banned until {conn.banned_until.isoformat()} (too many over-limit attempts)"
                # Already recorded when the ban was applied - not logged again.
            elif not conn.enabled:
                reason = "connection disabled"
                reject_kind = "disabled"
            else:
                user = conn.user
                # A connection created via "افزودن پکیج" carries its own
                # independent Purchase (see models.Purchase's docstring) -
                # its quota/expiry/status is checked INSTEAD of the user's
                # combined fields, exactly the same split
                # quota_manager.py's _enforce_purchase_limits/
                # _enforce_user_limits make. Every other connection (the
                # vast majority - anything from before this feature, or
                # from the normal create/bulk-create-with-package flows)
                # keeps checking the user's own fields, unchanged.
                purchase = conn.purchase if conn.purchase_id else None
                if purchase is not None:
                    quota_ok = not purchase.quota_bytes or purchase.used_bytes < purchase.quota_bytes
                    expiry_ok = not purchase.expire_at or purchase.expire_at > dt.datetime.utcnow()
                    if (not quota_ok or not expiry_ok) and _maybe_activate_reserved_purchase_renewal(db, purchase):
                        quota_ok = not purchase.quota_bytes or purchase.used_bytes < purchase.quota_bytes
                        expiry_ok = not purchase.expire_at or purchase.expire_at > dt.datetime.utcnow()
                    status_ok = purchase.status == models.UserStatus.active
                    reason_prefix = "purchase"
                else:
                    quota_ok = not user.total_quota_bytes or user.used_bytes < user.total_quota_bytes
                    expiry_ok = not user.expire_at or user.expire_at > dt.datetime.utcnow()
                    if (not quota_ok or not expiry_ok) and _maybe_activate_reserved_renewal(db, user):
                        # A reserved renewal (see User.reserved_quota_bytes's
                        # docstring) just kicked in - this login attempt should
                        # be judged against the fresh quota/expiry it just set,
                        # not the exhausted ones that triggered it, so this
                        # customer isn't needlessly rejected right at the
                        # boundary until the next poll cycle catches up.
                        quota_ok = not user.total_quota_bytes or user.used_bytes < user.total_quota_bytes
                        expiry_ok = not user.expire_at or user.expire_at > dt.datetime.utcnow()
                    status_ok = user.status == models.UserStatus.active
                    reason_prefix = "user"
                # A manual account-wide disable always applies regardless of
                # purchase - mirrors quota_manager.py's own
                # `if user.status == disabled: return` short-circuit.
                if user.status == models.UserStatus.disabled:
                    status_ok = False
                    reason_prefix = "user"
                if not status_ok:
                    effective_status = purchase.status if reason_prefix == "purchase" else user.status
                    reason = f"{reason_prefix} status={effective_status}"
                    # The status already says WHY it is not active, so the
                    # log says the same thing rather than a vague "status".
                    reject_kind = {
                        models.UserStatus.quota_exceeded: "quota_exceeded",
                        models.UserStatus.expired: "expired",
                        models.UserStatus.disabled: "disabled",
                    }.get(effective_status, "not_active")
                elif not quota_ok:
                    reason = "quota exceeded"
                    reject_kind = "quota_exceeded"
                elif not expiry_ok:
                    reason = "expired"
                    reject_kind = "expired"
                else:
                    ok = self._check_password(pkt, conn.ppp_password)
                    if not ok:
                        ok = self._check_mschapv2(pkt, reply, username, conn.ppp_password)
                    if not ok:
                        reason = "wrong password"
                        reject_kind = "auth_fail"
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
                                    client_ip=client_ip,
                                )
                            )
                            if just_banned and user.telegram_id:
                                _notify_ban_async(user.telegram_id, username, BAN_DURATION_MINUTES, conn.banned_until)
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
            if ok and conn is not None and conn.speed_limit_mbps:
                # See models.Connection.speed_limit_mbps's docstring - unlike
                # WireGuard's RouterOS Simple Queue (a persistent object kept
                # in sync from routers/users.py's update_connection), the PPP
                # protocols get this re-delivered fresh on every single
                # successful auth, straight in the Access-Accept, so there's
                # nothing to provision/clean up ahead of time and a limit
                # change takes effect on the connection's next (re)connect
                # with zero extra code. Format is "rx-rate/tx-rate" per
                # RouterOS's own RADIUS docs - rx/tx from the CLIENT's
                # perspective (rx = client upload, tx = client download);
                # this panel only ever offers one combined cap, so both
                # sides get the same value.
                rate = f"{conn.speed_limit_mbps}M/{conn.speed_limit_mbps}M"
                reply.AddAttribute(
                    "Vendor-Specific",
                    mschapv2.build_vsa(MIKROTIK_RATE_LIMIT_ATTR, rate.encode("ascii"), vendor_id=MIKROTIK_VENDOR_ID),
                )
            # Why this login failed, recorded where the admin can see it.
            #
            # Every reason below was already computed - it just went to the
            # container's stdout, which means an admin answering "why can't
            # my customer connect?" had to SSH in and grep. The
            # concurrent-limit branch above has been persisting its own
            # events for a while; these are the rest of them.
            #
            # The over-limit branch writes its own richer row (with counts
            # and the ban), so it is skipped here rather than logged twice.
            if not ok:
                kind = reject_kind
                # Counted BEFORE the log de-dup gate below, deliberately -
                # see ip_guard.record_radius_reject's docstring. The log
                # page collapses a retry storm into one row every 10
                # minutes; the ban counter must see every single one of
                # those retries or it would take ~100 minutes to ever trip.
                if kind and client_ip:
                    ip_guard.record_radius_reject(db, client_ip, kind)
                if kind and self._should_log_rejection(f"{kind}:{username}:{client_ip}"):
                    owner_id = None
                    if conn is not None and conn.user is not None:
                        owner_id = conn.user.owner_admin_id
                    db.add(models.RadiusLimitEventLog(
                        connection_id=conn.id if conn is not None else None,
                        user_id=conn.user_id if conn is not None else None,
                        owner_admin_id=owner_id,
                        username=username,
                        connection_type=(
                            (conn.type.value if hasattr(conn.type, "value") else str(conn.type))
                            if conn is not None else None
                        ),
                        event_type=kind,
                        client_ip=client_ip,
                    ))

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
    def _open_active_session(db, connection_id: int, session_id: str, nas_ip, client_ip=None) -> None:
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
            existing.client_ip = _to_str(client_ip) or existing.client_ip
            return
        db.add(
            models.RadiusActiveSession(
                connection_id=connection_id,
                session_id=session_id,
                nas_ip=_to_str(nas_ip) or None,
                client_ip=_to_str(client_ip) or None,
            )
        )

    @staticmethod
    def _touch_active_session(db, connection_id: int, session_id: str, nas_ip, client_ip=None) -> None:
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
            existing.client_ip = _to_str(client_ip) or existing.client_ip
        else:
            # Missed the Start packet - create it now so the concurrent-limit
            # count stays accurate.
            db.add(
                models.RadiusActiveSession(
                    connection_id=connection_id,
                    session_id=session_id,
                    nas_ip=_to_str(nas_ip) or None,
                    client_ip=_to_str(client_ip) or None,
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
            # The client's real remote IP, when the NAS sends it - same
            # attribute HandleAuthPacket already reads for the ban-log's
            # client_ip, reused here so it can also be shown live next to
            # the آنلاین badge (see RadiusActiveSession.client_ip).
            client_ip = _to_str(pkt.get("Calling-Station-Id", [None])[0])

            if conn:
                if status == "Start":
                    conn.radius_session_id = session_id
                    conn.last_rx_bytes = 0
                    conn.last_tx_bytes = 0
                    self._open_active_session(db, conn.id, session_id, nas_ip, client_ip)
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
                        self._touch_active_session(db, conn.id, session_id, nas_ip, client_ip)

                user = conn.user
                if user:
                    _enforce_user_limits(db, user)
                if conn.purchase_id:
                    # This connection's usage was just added to its OWN
                    # Purchase's used_bytes (see _apply_delta) - check that
                    # purchase's own exhaustion right away too, instead of
                    # waiting for the next periodic poll cycle.
                    _enforce_purchase_limits(db, conn.purchase)
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


# کاربر: "لاگ‌های محدودیت اتصال فقط برای یک هفته نگه داشته بشه کافیه" - قبلا
# models.RadiusLimitEventLog (رد شدن به‌خاطر عبور از سقف اتصال هم‌زمان + بن
# موقت پس از تلاش مکرر - نوشته می‌شه توسط HandleAuthPacket پایین همین فایل
# و routers/users.py's kick_connection) هیچ پاک‌سازی‌ای نداشت و برای همیشه
# انباشته می‌شد.
RADIUS_LIMIT_EVENT_LOG_KEEP_DAYS = 7


def cleanup_old_radius_limit_logs(keep_days: int = RADIUS_LIMIT_EVENT_LOG_KEEP_DAYS) -> int:
    """Deletes RadiusLimitEventLog rows older than `keep_days` - meant to be
    called once a day from the scheduler (see main.py's _start_full_services).
    Returns the number of rows deleted."""
    db = SessionLocal()
    try:
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=keep_days)
        deleted = (
            db.query(models.RadiusLimitEventLog)
            .filter(models.RadiusLimitEventLog.created_at < cutoff)
            .delete(synchronize_session=False)
        )
        db.commit()
        if deleted:
            logger.info("cleaned up %d old RADIUS limit event log(s) (older than %d day(s))", deleted, keep_days)
        return deleted
    except Exception:
        logger.exception("failed to clean up old RADIUS limit event logs")
        db.rollback()
        return 0
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
