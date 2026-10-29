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

import hashlib
import logging
import os
import secrets
import threading
import time
import uuid
import datetime as dt

from pyrad import packet
from pyrad.dictionary import Dictionary
from pyrad.server import Server, RemoteHost

from .. import models
from ..config import settings
from ..database import SessionLocal
from ..telegram_bot import runner as telegram_bot_runner
from .quota_manager import add_usage, _enforce_user_limits, _enforce_purchase_limits, active_session_count
from . import session_cap_mode, session_cap_rule, license_guard, reseller_refund_block
from .user_ops import _maybe_activate_reserved_renewal, _maybe_activate_reserved_purchase_renewal
from .first_use import activate_connection_expiry
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
# How long a closed session is remembered against late/retransmitted accounting.
CLOSED_SESSION_KEEP_DAYS = 3
# Our Class cookie (models.RadiusAuthGrant): any other Class value is not ours.
CLASS_PREFIX = "um1:"


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


def _verified_counter(pkt, octets_attr, gigawords_attr):
    """A Class-bound final sample must contain a real uint32 low word.

    An absent high word is legal below the first wrap; a decreased final
    total is rejected against the grant's high-water mark separately.
    Missing/invalid values never stand in for literal zero.
    """
    try:
        low_values = pkt[octets_attr]
        high_values = pkt[gigawords_attr] if gigawords_attr in pkt else [0]
        if len(low_values) != 1 or len(high_values) != 1:
            return None
        low, high = low_values[0], high_values[0]
        if type(low) is not int or type(high) is not int or not (0 <= low < 2**32 and 0 <= high < 2**32):
            return None
        # SQL BigInteger is signed on supported databases.
        total = low + high * 2**32
        return total if total < 2**63 else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


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



def _legacy_session_limit(db, user, conn):
    """(limit, active count) of the legacy rule - unchanged."""
    if user.max_concurrent_sessions:
        # User-level cap: counts currently-active
        # connections across ALL of this user's services
        # combined (e.g. an OpenVPN server + a WireGuard
        # peer + a VLESS account bundled from one
        # package), not just PPP ones. This can only
        # ever REJECT a new PPP login attempt (the one
        # live enforcement point this panel has for a
        # login event); it can't kick an already-open
        # xray/wireguard session in real time - that's
        # what quota_manager.enforce_concurrent_session_
        # limits is for, reactively, on the next poll
        # cycle. Shared counting logic - see
        # quota_manager.active_session_count's own
        # docstring for exactly how each protocol type
        # is counted.
        limit = user.max_concurrent_sessions
        active_count = active_session_count(db, user.id)
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
    return limit, active_count


def _apply_session_cap_mode(db, user, conn, mode, legacy):
    """enforced: the per-Purchase rule's (limit, count)
    (session_cap_rule.session_scope). shadow: the legacy pair, unchanged,
    after recording where the per-Purchase rule would decide otherwise -
    a failure to compare never changes the legacy decision."""
    if mode == "enforced":
        limit, scope = session_cap_rule.session_scope(user, conn)
        return limit, (session_cap_rule.scope_session_count(db, scope) if limit else 0)
    try:
        limit, scope = session_cap_rule.session_scope(user, conn)
        count = session_cap_rule.scope_session_count(db, scope) if limit else 0
        legacy_rejects = bool(legacy[0] and legacy[1] >= legacy[0])
        new_rejects = bool(limit and count >= limit)
        if legacy_rejects != new_rejects:
            session_cap_mode.note("radius_would_reject" if new_rejects else "radius_would_accept",
                                  user_id=user.id, purchase_id=conn.purchase_id, connection_id=conn.id,
                                  legacy=legacy, new=(limit, count))
    except Exception:  # noqa: BLE001
        logger.exception("session cap shadow comparison failed")
    return legacy

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
        """One Access-Request, decided and answered under the shared lock of
        session_cap_mode from before the mode is read until after the reply
        is sent - a mode change can never fall in between. No valid mode
        (lock busy or unavailable, mode unreadable): no decision and no reply,
        exactly as for any other internal error here; the NAS retries.

        A queued renewal this login activates is a DB change only here: the
        node reconciliation it needs (remote writes, the cancellation
        barrier) runs after the reply, with neither this lock nor a database
        transaction held (reservation_transition.deferred_reconciliation) -
        a pending cancellation's block_ppp waiting for this lock can no
        longer wait on a barrier held by that same cancellation."""
        from . import reservation_transition
        with reservation_transition.deferred_reconciliation() as pending:
            try:
                with session_cap_mode.decision(SessionLocal.kw["bind"]) as cap_mode:
                    self._handle_auth_packet(pkt, cap_mode)
            except session_cap_mode.ModeUnavailable as exc:
                logger.error("RADIUS Access-Request not answered: no session cap mode (%s)", exc)
        reservation_transition.run_deferred(pending, SessionLocal.kw["bind"])

    def _handle_auth_packet(self, pkt, cap_mode):
        db = SessionLocal()
        try:
            reply = self.CreateReplyPacket(pkt)
            if license_guard.current().cut_services:
                # Licence scope is independent of customer quota, bans and
                # accounting: do not punish the client's IP or consume a
                # queued renewal while service access is suspended.
                reply.code = packet.AccessReject
                _send_reply(self, pkt, reply)
                return
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
            elif reseller_refund_block.ppp_blocked(db, conn.id):
                # Its unpaid-service cancellation is under way: no new
                # session may start before its final usage is known.
                reason = "service cancellation in progress"
                reject_kind = "disabled"
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
                    if (not quota_ok or not expiry_ok) and _maybe_activate_reserved_purchase_renewal(db, purchase, "radius"):
                        # its guard runs at the flush: a renewal void's hold
                        # committed meanwhile puts the purchase back, and this
                        # login is judged on that (no node call follows here)
                        db.flush()
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
                        # Which rule decides is session_cap_mode's (read under
                        # the lock HandleAuthPacket holds until the reply):
                        # legacy (default) is _legacy_session_limit, exactly as
                        # it always was; enforced is the per-Purchase rule;
                        # shadow decides by legacy and records the difference.
                        limit, active_count = _legacy_session_limit(db, user, conn)
                        if cap_mode != "legacy":
                            limit, active_count = _apply_session_cap_mode(
                                db, user, conn, cap_mode, (limit, active_count))
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
                        if ok and conn is not None:
                            # Start the allotment this connection belongs to:
                            # its Purchase when present, or the legacy User.
                            activate_connection_expiry(db, conn)
            if ok and conn is not None and settings.reseller_cancellation_enabled:
                # Durable (committed below) before the Accept is sent, and its
                # cookie in the Accept: the one session it admits is known by
                # it from its first Accounting-Request on (models.RadiusAuthGrant).
                # Only while cancellation can run - nothing new with it off.
                # A request from no single configured node (mt_host a hostname,
                # shared) gets no cookie: its session is accounted as before
                # (by id) and stays unverifiable for a cancellation.
                # Its grant is still written (node unknown): a cancellation of
                # this connection then fails closed (ppp_session_foreign_node)
                # instead of never knowing this session existed.
                trusted = self._trusted_node(db, pkt)
                grant = self._grant(db, pkt, conn, trusted)
                if trusted is not None:
                    reply.AddAttribute("Class", grant.nonce.encode())
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
                models.RadiusActiveSession.auth_nonce.is_(None),
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
                models.RadiusActiveSession.auth_nonce.is_(None),
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
    def _closed_session(db, connection_id: int, session_id):
        """The remembered Stop of this id (an open session may reuse it)."""
        if not session_id:
            return None
        return db.query(models.RadiusClosedSession).filter_by(
            connection_id=connection_id, session_id=session_id).first()

    @staticmethod
    def _is_final_replay(closed, in_octets: int, out_octets: int, seconds) -> bool:
        """The closed session's own final report again (a retransmitted Stop)."""
        return ((in_octets, out_octets) == (int(closed.final_rx_bytes or 0), int(closed.final_tx_bytes or 0))
                and seconds == closed.final_session_time)

    @staticmethod
    def _proves_new_session(closed, in_octets: int, out_octets: int, seconds) -> bool:
        """An Interim that cannot belong to the closed session under its id:
        a counter or Acct-Session-Time above that session's final ones.
        Anything else is ambiguous - a late Interim of the closed session
        (however late it arrives), or an early one of a new session after a
        reboot - and is NOT counted: the new session's next report is
        deltaed from the same zero baseline, so nothing it used is lost,
        while the closed session is never counted twice. No packet arrival
        time is used as proof."""
        if in_octets > int(closed.final_rx_bytes or 0) or out_octets > int(closed.final_tx_bytes or 0):
            return True
        return (seconds is not None and closed.final_session_time is not None
                and seconds > closed.final_session_time)

    # ------------------------------------------------ Class-bound sessions (models.RadiusAuthGrant)
    @staticmethod
    def _trusted_node(db, pkt):
        """The node whose configured mt_host this packet came from - the
        address pyrad authenticated it with (its shared secret, refresh_hosts);
        never a NAS attribute inside the packet. None if not exactly one."""
        source = pkt.source[0] if getattr(pkt, "source", None) else None
        if not source:
            return None
        nodes = db.query(models.Node).filter(models.Node.type == models.NodeType.mikrotik,
                                             models.Node.mt_host == source).limit(2).all()
        return nodes[0] if len(nodes) == 1 else None

    @staticmethod
    def _grant(db, pkt, conn, node) -> models.RadiusAuthGrant:
        """The grant (and Class nonce) of this Access-Request: a retransmission
        of it (same NAS, packet id and authenticator) gets the same one."""
        if conn.history_key is None:
            conn.history_key = str(uuid.uuid4())
        identity = models.radius_connection_identity(conn)
        raw = "|".join((str(node.id if node else None), pkt.source[0] if getattr(pkt, "source", None) else "",
                        str(pkt.id), bytes(pkt.authenticator or b"").hex(), str(conn.id), identity))
        key = hashlib.sha256(raw.encode()).hexdigest()
        grant = db.query(models.RadiusAuthGrant).filter_by(request_key=key).first()
        if grant is None:
            purchase = conn.purchase if conn.purchase_id else None
            grant = models.RadiusAuthGrant(
                nonce=CLASS_PREFIX + secrets.token_hex(16), request_key=key, connection_id=conn.id,
                connection_history_key=conn.history_key,
                connection_identity_digest=identity,
                purchase_id=conn.purchase_id, purchase_key=purchase.history_key if purchase else None,
                node_id=node.id if node else None, nas_ip=pkt.source[0] if getattr(pkt, "source", None) else None,
                # RouterOS sends the coming session's id in the Access-Request
                # (observed on real CHR L2TP/PPTP): the session is named from
                # the start, even if its accounting Start is lost.
                acct_session_id=_to_str(pkt.get("Acct-Session-Id", [None])[0]) or None)
            db.add(grant)
        return grant

    @staticmethod
    def _class_nonce(pkt):
        """Our Class cookie in an Accounting-Request, or None (none, or not ours)."""
        try:
            values = pkt["Class"] if "Class" in pkt else []
        except Exception:
            return None
        for value in values:
            text = value.decode(errors="ignore") if isinstance(value, bytes) else str(value)
            if text.startswith(CLASS_PREFIX):
                return text
        return None

    def _account_grant(self, db, pkt, conn, nonce, status, session_id, in_octets, out_octets, client_ip) -> bool:
        """Accounting of a Class-bound session. False: the cookie names no
        grant this packet may act on (unknown, another node, another
        connection or purchase history) - nothing is changed by it."""
        grant = db.query(models.RadiusAuthGrant).filter_by(nonce=nonce).first()
        node = self._trusted_node(db, pkt)
        identity = models.radius_connection_identity(conn)
        if (grant is None or node is None or grant.node_id != node.id or grant.connection_id != conn.id
                or identity is None or grant.connection_history_key != conn.history_key
                or grant.connection_identity_digest != identity):
            # (conn is the connection of this packet's User-Name: same id and
            # same credential - not another service under a reused id)
            logger.warning("RADIUS Acct %s with foreign Class for connection %s ignored", status, conn.id)
            return False
        if grant.state == "stopped":
            # A completed grant's provenance is historical, immutable evidence;
            # a replay after a later purchase migration must not rebind it.
            logger.info("RADIUS Acct %s for ended session %s (grant %s) ignored", status, session_id, grant.id)
            return True
        purchase = conn.purchase if conn.purchase_id else None
        current_key = purchase.history_key if purchase else None
        if grant.purchase_id != conn.purchase_id or grant.purchase_key != current_key:
            # The connection was moved to another purchase (or its history
            # key backfilled) while this session is up: its traffic is still
            # this credential's - counted on it, the grant follows it.
            logger.info("RADIUS grant %s re-bound: connection %s now on purchase %s", grant.id, conn.id,
                        conn.purchase_id)
            grant.purchase_id, grant.purchase_key = conn.purchase_id, current_key
        if session_id and grant.acct_session_id and session_id != grant.acct_session_id:
            if grant.state == "granted":
                # nothing counted under the Access-Request's id yet: the
                # accounting id is the session's (the cookie is the identity)
                grant.acct_session_id = session_id
            else:
                logger.warning("RADIUS Acct %s: Class of grant %s under another session id - ignored",
                               status, grant.id)
                return False                 # not acted on (a held connection: unverified)
        grant.acct_session_id = grant.acct_session_id or session_id or None
        if status in ("Interim-Update", "Stop") and (
                in_octets is None or out_octets is None or (status == "Stop" and (
                    in_octets < int(grant.last_rx_bytes or 0) or out_octets < int(grant.last_tx_bytes or 0)))):
            # This report cannot prove final usage. Keep the grant unended
            # (and existing session row); a corrected complete Stop can retry.
            grant.accounting_unverified = True
            return True
        if status == "Interim-Update" and (in_octets < int(grant.last_rx_bytes or 0)
                                           or out_octets < int(grant.last_tx_bytes or 0)):
            # A complete Interim below the high-water mark: wrap, reset or a
            # reordered report - not knowable here. Kept for good (no later
            # Stop clears it); nothing is added or guessed for it below.
            logger.warning("RADIUS Acct Interim of grant %s: counter below its high-water mark", grant.id)
            grant.counter_regressed_at = grant.counter_regressed_at or dt.datetime.utcnow()
        # Start commonly has no counters and is not final usage evidence.
        in_octets, out_octets = in_octets or 0, out_octets or 0
        # High-water marks per direction: a delayed or reordered report never adds again.
        up = max(0, in_octets - int(grant.last_rx_bytes or 0))
        down = max(0, out_octets - int(grant.last_tx_bytes or 0))
        if up or down:
            # counters above anything reported before: the session is alive
            # since its NAS restarted (a late pre-restart report proves nothing)
            grant.nas_restart_at = None
        grant.last_rx_bytes = max(int(grant.last_rx_bytes or 0), in_octets)
        grant.last_tx_bytes = max(int(grant.last_tx_bytes or 0), out_octets)
        if up or down:
            add_usage(db, conn, down + up, down, up)
        row = db.query(models.RadiusActiveSession).filter_by(auth_nonce=nonce).first()
        if status == "Stop":
            grant.accounting_unverified = False
            grant.state, grant.stopped_at = "stopped", dt.datetime.utcnow()
            conn.radius_session_id = None
            if row is not None:
                db.delete(row)
            return True
        grant.state = "open"
        conn.radius_session_id = session_id
        if row is None:
            row = models.RadiusActiveSession(connection_id=conn.id, session_id=session_id or grant.nonce,
                                             nas_ip=grant.nas_ip, client_ip=_to_str(client_ip) or None,
                                             auth_nonce=nonce)
            db.add(row)
        row.last_seen_at = dt.datetime.utcnow()
        row.client_ip = _to_str(client_ip) or row.client_ip
        row.last_rx_bytes, row.last_tx_bytes = grant.last_rx_bytes, grant.last_tx_bytes
        return True

    def _nas_restarted(self, db, pkt) -> None:
        """Accounting-On/Off from a trusted NAS: recorded on its unended
        grants (nas_restart_at) as EVIDENCE only - accounting with a valid
        cookie still counts (the auth and accounting sockets are not ordered:
        an Accept for a session after the reboot can be decided before this
        packet is handled). An open session's usage since its last report
        may be lost: such a grant settles no cancellation (ppp_stop_missing)
        until a later report of it proves it alive. Nothing for another node."""
        node = self._trusted_node(db, pkt)
        if node is None:
            return
        now = dt.datetime.utcnow()
        for grant in db.query(models.RadiusAuthGrant).filter(
                models.RadiusAuthGrant.node_id == node.id,
                models.RadiusAuthGrant.state.in_(("granted", "open"))):
            grant.nas_restart_at = now

    @staticmethod
    def _remember_closed(db, connection_id: int, session_id: str, in_octets: int, out_octets: int, seconds) -> None:
        """Upsert: a reused id's Stop updates its row (a second INSERT failed on
        the unique key and rolled the whole Stop back)."""
        row = db.query(models.RadiusClosedSession).filter_by(
            connection_id=connection_id, session_id=session_id).first()
        if row is None:
            row = models.RadiusClosedSession(connection_id=connection_id, session_id=session_id)
            db.add(row)
        row.closed_at = dt.datetime.utcnow()
        row.final_rx_bytes, row.final_tx_bytes, row.final_session_time = in_octets, out_octets, seconds

    @staticmethod
    def _close_active_session(db, connection_id: int, session_id: str) -> None:
        """Removes the session's row THROUGH the session, not with a bulk
        DELETE. _session_delta has usually just changed that same row, and
        our sessions run with autoflush off: a bulk DELETE left that UPDATE
        pending, the commit then updated a row that was already gone
        (StaleDataError) and rolled the whole Stop back - its final usage
        was never counted and the row stayed as a phantom online session.
        A row _session_delta only just created (we never saw the Start) is
        still pending and is simply dropped again."""
        for obj in list(db.new):
            if (isinstance(obj, models.RadiusActiveSession) and obj.connection_id == connection_id
                    and obj.session_id == session_id and obj.auth_nonce is None):
                db.expunge(obj)
        for row in db.query(models.RadiusActiveSession).filter(
            models.RadiusActiveSession.connection_id == connection_id,
            models.RadiusActiveSession.session_id == session_id,
            models.RadiusActiveSession.auth_nonce.is_(None),
        ).all():
            db.delete(row)

    @staticmethod
    def _session_delta(db, connection_id: int, session_id: str, in_octets: int, out_octets: int) -> tuple[int, int]:
        """How many bytes THIS session has moved since its own last report,
        as (download_delta, upload_delta).

        RADIUS counters are per session and start at zero, so the baseline
        has to be per session too. It used to live on the Connection, which
        is the same thing only while a connection has ONE session - and a
        «۳ کاربر همزمان» package exists precisely so that it can have
        three. With two, each report found a baseline belonging to the
        other session, decided the counter had "reset", and added that
        session's entire cumulative total again. Two devices, 1.4 GB really
        used, 3.6 GB recorded.

        A row is created if we never saw the Start - a fresh baseline for
        that session only, leaving every other session's untouched.

        Each direction is deltaed against its OWN previous value, not a
        combined total - a combined check can miss one counter resetting
        alone while the other keeps growing (see _apply_delta's docstring
        in quota_manager.py for the same fix there). Acct-Input-Octets is
        what the NAS received FROM the client (the client's upload),
        Acct-Output-Octets is what it sent TO the client (the client's
        download) - hence (download, upload) = (out_delta, in_delta).
        """
        row = (
            db.query(models.RadiusActiveSession)
            .filter(
                models.RadiusActiveSession.connection_id == connection_id,
                models.RadiusActiveSession.session_id == session_id,
                models.RadiusActiveSession.auth_nonce.is_(None),
            )
            .first()
        )
        if row is None:
            row = models.RadiusActiveSession(
                connection_id=connection_id, session_id=session_id,
                last_rx_bytes=0, last_tx_bytes=0,
            )
            db.add(row)

        prev_in = row.last_rx_bytes or 0
        prev_out = row.last_tx_bytes or 0
        in_octets = in_octets or 0
        out_octets = out_octets or 0
        # Within one session a counter only ever grows; going backwards
        # means the NAS restarted the session under the same id, so what it
        # reports now is all of it.
        in_delta = in_octets - prev_in if in_octets >= prev_in else in_octets
        out_delta = out_octets - prev_out if out_octets >= prev_out else out_octets
        row.last_rx_bytes = in_octets
        row.last_tx_bytes = out_octets
        return max(0, out_delta), max(0, in_delta)

    # ---------------------------------------------------------- accounting
    def HandleAcctPacket(self, pkt):
        """A queued renewal its usage activates reconciles its nodes after
        this packet's transaction (reservation_transition), never inside it.
        (Quota/expiry enforcement's own enable/disable toggles still run in
        it, as before - quota_manager._set_connection_enabled.)"""
        from . import reservation_transition
        with reservation_transition.deferred_reconciliation() as pending:
            self._handle_acct_packet(pkt)
        reservation_transition.run_deferred(pending, SessionLocal.kw["bind"])

    def _handle_acct_packet(self, pkt):
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
            nonce = self._class_nonce(pkt)

            if status in ("Accounting-On", "Accounting-Off"):
                self._nas_restarted(db, pkt)
                db.commit()
            elif conn and nonce is not None:
                # A Class-bound session: accounted by its grant alone; a
                # foreign or unknown cookie changes nothing (not even by id).
                in_octets = _verified_counter(pkt, "Acct-Input-Octets", "Acct-Input-Gigawords")
                out_octets = _verified_counter(pkt, "Acct-Output-Octets", "Acct-Output-Gigawords")
                if status in ("Start", "Interim-Update", "Stop") and self._account_grant(
                        db, pkt, conn, nonce, status, session_id, in_octets, out_octets, client_ip):
                    if conn.user:
                        _enforce_user_limits(db, conn.user)
                    if conn.purchase_id:
                        _enforce_purchase_limits(db, conn.purchase)
                elif reseller_refund_block.ppp_blocked(db, conn.id):
                    reseller_refund_block.note_unverified(db, conn.id)
                db.commit()
            elif conn:
                if status in ("Start", "Interim-Update", "Stop") and reseller_refund_block.ppp_blocked(db, conn.id):
                    # a session without our cookie on a held connection: its
                    # identity cannot be verified - the cancellation fails closed
                    reseller_refund_block.note_unverified(db, conn.id)
                closed = self._closed_session(db, conn.id, session_id) if status in (
                    "Start", "Interim-Update", "Stop") else None
                try:
                    seconds = int(pkt["Acct-Session-Time"][0]) if "Acct-Session-Time" in pkt else None
                except Exception:
                    seconds = None
                in_octets = _gigaword_total(pkt, "Acct-Input-Octets", "Acct-Input-Gigawords")
                out_octets = _gigaword_total(pkt, "Acct-Output-Octets", "Acct-Output-Gigawords")
                row = db.query(models.RadiusActiveSession).filter_by(
                    connection_id=conn.id, session_id=session_id, auth_nonce=None).first() if closed is not None else None
                fresh = row is None or not (row.last_rx_bytes or row.last_tx_bytes)
                if status == "Start":
                    # Always opened - even right after this id's Stop: a late
                    # Start of that session and a new session after a reboot
                    # look the same here. What follows decides (below).
                    conn.radius_session_id = session_id
                    self._open_active_session(db, conn.id, session_id, nas_ip, client_ip)
                elif status in ("Interim-Update", "Stop") and closed is not None and self._is_final_replay(
                        closed, in_octets, out_octets, seconds):
                    # The closed session's Stop retransmitted - counted once
                    # already. A row with no report yet under the id was
                    # that session's late Start: no session is open.
                    if row is not None and fresh:
                        self._close_active_session(db, conn.id, session_id)
                    logger.info("RADIUS Acct %s replays closed session %s of connection %s - ignored",
                                status, session_id, conn.id)
                elif (status == "Interim-Update" and closed is not None and fresh
                      and not self._proves_new_session(closed, in_octets, out_octets, seconds)):
                    if row is not None:
                        row.last_seen_at = dt.datetime.utcnow()   # still alive, whichever session it is
                    logger.info("RADIUS Acct Interim for closed session id %s of connection %s not counted "
                                "(ambiguous; the next report counts it if it is a new session)",
                                session_id, conn.id)
                elif status in ("Interim-Update", "Stop"):
                    # A Stop other than the closed one's own final report is a
                    # session's one Stop: a new session under the id.
                    conn.radius_session_id = session_id
                    # Per session, not per connection. The delta is worked
                    # out against THIS session's own previous report; every
                    # other session on the same connection is untouched.
                    download_delta, upload_delta = self._session_delta(db, conn.id, session_id, in_octets, out_octets)
                    add_usage(db, conn, download_delta + upload_delta, download_delta, upload_delta)
                    if status == "Stop":
                        conn.radius_session_id = None
                        # After the delta above, never before - closing
                        # deletes the row the baseline lives on, and the
                        # final report is usually the largest one.
                        self._close_active_session(db, conn.id, session_id)
                        if session_id:
                            self._remember_closed(db, conn.id, session_id, in_octets, out_octets, seconds)
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
        query = db.query(models.RadiusActiveSession).filter(models.RadiusActiveSession.last_seen_at < cutoff)
        # A connection held by an unpaid-service cancellation keeps its open
        # sessions: only their Accounting Stop proves the final usage, and a
        # pruned row would look exactly like a closed one.
        held = reseller_refund_block.blocked_connection_ids(db)
        if held:
            query = query.filter(models.RadiusActiveSession.connection_id.notin_(held))
        deleted = query.delete(synchronize_session=False)
        # A held connection's Stops are its cancellation's evidence
        # (reseller_refund_settlement.capture_ppp): kept until it settles.
        closed = db.query(models.RadiusClosedSession).filter(
            models.RadiusClosedSession.closed_at < dt.datetime.utcnow() - dt.timedelta(days=CLOSED_SESSION_KEEP_DAYS))
        if held:
            closed = closed.filter(models.RadiusClosedSession.connection_id.notin_(held))
        closed.delete(synchronize_session=False)
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
