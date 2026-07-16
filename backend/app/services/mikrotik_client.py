"""Thin wrapper around librouteros for managing WireGuard peers on a
MikroTik RouterOS (v7+) device, reading their traffic counters, and
(optionally) registering this panel as a RADIUS client on the router.

RouterOS exposes WireGuard peers under `/interface/wireguard/peers`. Each
peer entry carries `rx` / `tx` byte counters that only ever increase while
the peer exists (they reset if the peer is removed and re-created).

OpenVPN and L2TP users are authenticated via RADIUS against this panel's
own database instead of local PPP secrets (see app/services/radius_server.py)
- this client no longer touches `/ppp/secret` at all.
"""
from __future__ import annotations

import logging
import ssl
from typing import Optional

import librouteros
from librouteros.query import Key

logger = logging.getLogger("mikrotik_client")


class MikrotikError(Exception):
    pass


def parse_ros_duration_seconds(raw) -> Optional[float]:
    """Parses RouterOS's compact duration format (as returned in a
    WireGuard peer's `last-handshake` field, e.g. "3s", "5m30s", "1h2m3s",
    "1d2h3m4s") into a number of seconds. Used to work out whether a peer
    handshook recently enough to count as "currently connected" - RouterOS
    itself exposes no explicit online/offline flag for WireGuard, only this
    duration-since-last-handshake. Returns None if the peer has never
    handshaken (field missing/empty) or the value can't be parsed."""
    if not raw or not isinstance(raw, str):
        return None
    units = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    total = 0.0
    num = ""
    matched_any = False
    for ch in raw:
        if ch.isdigit() or ch == ".":
            num += ch
        elif ch in units and num:
            total += float(num) * units[ch]
            num = ""
            matched_any = True
        else:
            # Unexpected character (e.g. a "ms" fragment) - bail rather than
            # silently mis-parse.
            return None
    if not matched_any:
        return None
    return total


class MikrotikClient:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 8728,
        use_ssl: bool = False,
        timeout: int = 8,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.use_ssl = use_ssl
        self.timeout = timeout
        self._api = None

    @classmethod
    def for_node(cls, node) -> "MikrotikClient":
        """Builds a client from a Node row, automatically picking the plain
        API port or the API-SSL port depending on node.mt_use_ssl - both are
        independently editable from the panel."""
        port = node.mt_api_ssl_port if node.mt_use_ssl else node.mt_port
        return cls(node.mt_host, node.mt_username, node.mt_password, port, node.mt_use_ssl)

    def connect(self):
        try:
            kwargs = dict(
                host=self.host,
                username=self.username,
                password=self.password,
                port=self.port,
                timeout=self.timeout,
            )
            if self.use_ssl:
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
                kwargs["ssl_wrapper"] = context.wrap_socket
            self._api = librouteros.connect(**kwargs)
        except Exception as exc:  # pragma: no cover - network dependent
            raise MikrotikError(f"اتصال به میکروتیک برقرار نشد: {exc}") from exc
        return self

    def close(self):
        if self._api is not None:
            try:
                self._api.close()
            except Exception:
                pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------ WireGuard
    def ensure_wireguard_interface(self, name: str, listen_port: int = 13231):
        """Create the WireGuard server interface if it does not exist yet."""
        path = self._api.path("interface", "wireguard")
        existing = list(path.select(Key("name")).where(Key("name") == name))
        if existing:
            return
        try:
            path.add(name=name, **{"listen-port": listen_port})
        except Exception as exc:
            raise MikrotikError(f"ساخت اینترفیس WireGuard ناموفق بود: {exc}") from exc

    def ensure_interface_address(self, interface: str, address_with_prefix: str):
        """Makes sure the router itself has an IP on the WireGuard interface
        (e.g. 10.66.66.1/24), needed so it can route/NAT traffic to peers."""
        path = self._api.path("ip", "address")
        existing = list(path.select(Key("interface")).where(Key("interface") == interface))
        if existing:
            return
        try:
            path.add(address=address_with_prefix, interface=interface)
        except Exception as exc:
            raise MikrotikError(f"تنظیم آدرس IP روی اینترفیس WireGuard ناموفق بود: {exc}") from exc

    def set_interface_address(self, interface: str, address_with_prefix: str):
        """Forces the WireGuard interface's own IP/prefix to match
        address_with_prefix, replacing any existing address entry for that
        interface instead of skipping (unlike ensure_interface_address).
        Used when the client subnet was just auto-expanded (see
        services/user_ops.py's _wg_gateway_and_client_ip) - the gateway IP
        itself usually stays the same, but its prefix length grows, so the
        router's own /ip/address entry needs updating to keep routing/NAT
        consistent with the wider pool."""
        path = self._api.path("ip", "address")
        existing = list(path.select(Key(".id"), Key("interface")).where(Key("interface") == interface))
        try:
            for row in existing:
                path.remove(row[".id"])
            path.add(address=address_with_prefix, interface=interface)
        except Exception as exc:
            raise MikrotikError(f"به‌روزرسانی آدرس IP اینترفیس WireGuard ناموفق بود: {exc}") from exc

    def list_peers(self, interface: Optional[str] = None) -> list[dict]:
        path = self._api.path("interface", "wireguard", "peers")
        rows = list(path)
        if interface:
            rows = [r for r in rows if r.get("interface") == interface]
        return rows

    def add_peer(
        self,
        interface: str,
        public_key: str,
        allowed_address: str,
        comment: str,
    ) -> str:
        """Adds a peer to the WireGuard interface. Returns RouterOS `.id`."""
        path = self._api.path("interface", "wireguard", "peers")
        try:
            result = path.add(
                interface=interface,
                **{
                    "public-key": public_key,
                    "allowed-address": allowed_address,
                    "comment": comment,
                },
            )
            return result
        except Exception as exc:
            raise MikrotikError(f"افزودن peer در میکروتیک ناموفق بود: {exc}") from exc

    def rename_peer(self, interface: str, old_comment: str, new_comment: str):
        """Finds the WireGuard peer by its current comment (the panel has no
        stored RouterOS `.id` for peers - see deprovision_connection in
        services/user_ops.py, which matches the same way) and updates its
        comment to new_comment. Used when an admin renames a connection's
        peer name from the panel (routers/users.py's update_connection) -
        keeps the router-side comment in sync so future lookups by comment
        (delete/disable) keep matching the right peer."""
        if old_comment == new_comment:
            return
        path = self._api.path("interface", "wireguard", "peers")
        peers = list(path.select(Key(".id"), Key("comment")).where(Key("interface") == interface))
        match = next((p for p in peers if p.get("comment") == old_comment), None)
        if not match:
            return  # nothing on the router to sync - DB rename still applies
        try:
            path.update(**{".id": match[".id"], "comment": new_comment})
        except Exception as exc:
            raise MikrotikError(f"تغییر نام peer در میکروتیک ناموفق بود: {exc}") from exc

    def set_peer_disabled(self, peer_id: str, disabled: bool):
        path = self._api.path("interface", "wireguard", "peers")
        try:
            path.update(**{".id": peer_id, "disabled": "yes" if disabled else "no"})
        except Exception as exc:
            raise MikrotikError(f"تغییر وضعیت peer ناموفق بود: {exc}") from exc

    def remove_peer(self, peer_id: str):
        path = self._api.path("interface", "wireguard", "peers")
        try:
            path.remove(peer_id)
        except Exception as exc:
            raise MikrotikError(f"حذف peer ناموفق بود: {exc}") from exc

    def get_public_key(self, interface: str) -> Optional[str]:
        """Returns the server's own public key for the given WG interface,
        needed to build client configs."""
        path = self._api.path("interface", "wireguard")
        rows = list(path.select(Key("name"), Key("public-key")).where(Key("name") == interface))
        if rows:
            return rows[0].get("public-key")
        return None

    # --------------------------------------------------------- kick session
    def kick_ppp_session(self, ppp_username: str) -> bool:
        """Force-closes a currently-open OpenVPN/L2TP/IKEv2/SSTP session by
        removing its entry from RouterOS's own `/ppp/active` table - this is
        the live list of sessions the router itself is holding, independent
        of anything in this panel's database. Works without any extra
        RADIUS CoA/Disconnect-Request setup on the router (which would need
        `/radius incoming accept=yes` and isn't assumed to be configured).
        Returns True if a matching active session was found and removed,
        False if the user simply wasn't connected right now (not an error -
        the caller should treat this as a no-op, not a failure)."""
        path = self._api.path("ppp", "active")
        try:
            rows = list(path.select(Key(".id"), Key("name")).where(Key("name") == ppp_username))
        except Exception as exc:
            raise MikrotikError(f"خواندن سشن‌های فعال PPP ناموفق بود: {exc}") from exc
        if not rows:
            return False
        try:
            for row in rows:
                path.remove(row[".id"])
        except Exception as exc:
            raise MikrotikError(f"قطع سشن ناموفق بود: {exc}") from exc
        return True

    def list_active_ppp_usernames(self) -> set[str]:
        """Returns the set of usernames RouterOS itself currently lists in
        `/ppp/active` - the router's own ground truth for who's really
        connected right now, independent of anything RADIUS accounting has
        told this panel. Used by quota_manager's poll cycle to close out any
        RadiusActiveSession row that accounting alone left dangling (e.g. a
        missed Stop packet, or a session that was already open on the
        router before its user was ever imported into the panel - see
        radius_server.py's `_touch_active_session` "missed the Start"
        fallback, which has no way to know on its own whether the session
        it just recorded is still actually up)."""
        path = self._api.path("ppp", "active")
        try:
            rows = list(path.select(Key("name")))
        except Exception as exc:
            raise MikrotikError(f"خواندن سشن‌های فعال PPP ناموفق بود: {exc}") from exc
        return {row.get("name") for row in rows if row.get("name")}

    # ------------------------------------------------------------- RADIUS
    # OpenVPN/L2TP users are now authenticated via RADIUS (this panel runs
    # its own RADIUS server - see app/services/radius_server.py) instead of
    # local PPP secrets. The panel does NOT touch the OpenVPN/L2TP server,
    # certificates or IPsec config on the router - those remain fully
    # manual. The one exception, added at the admin's explicit request, is
    # this helper: it registers the panel as a `/radius` client on the
    # router and flips `ppp aaa` to use it, so the admin doesn't have to
    # type those two RouterOS commands by hand.
    #
    # `service` matters: "ppp" covers OpenVPN/L2TP/SSTP's own PPP login
    # (username/password straight to RADIUS, incl. our MS-CHAPv2 support -
    # see services/mschapv2.py). "ipsec" is a SEPARATE RADIUS client entry
    # RouterOS uses only for IKEv2's EAP-over-RADIUS login (see
    # push_ikev2_config below) - registering only "ppp" is not enough to
    # make IKEv2 itself authenticate against this panel.
    def _ensure_radius_client(
        self,
        panel_host: str,
        secret: str,
        auth_port: int,
        acct_port: int,
        service: str,
    ) -> None:
        path = self._api.path("radius")
        existing = list(
            path.select(Key(".id"), Key("address"), Key("service"))
            .where(Key("address") == panel_host, Key("service") == service)
        )
        props = {
            "service": service,
            "address": panel_host,
            "secret": secret,
            "authentication-port": auth_port,
            "accounting-port": acct_port,
        }
        if existing:
            path.update(**{".id": existing[0][".id"]}, **props)
        else:
            path.add(**props)

    def push_radius_config(
        self,
        panel_host: str,
        secret: str,
        auth_port: int,
        acct_port: int,
        service: str = "ppp",
        interim_update: str = "00:05:00",
    ) -> None:
        try:
            self._ensure_radius_client(panel_host, secret, auth_port, acct_port, service)
            # /ppp/aaa is a singleton settings menu (no .id) - "set" applies
            # directly, equivalent to: /ppp aaa set use-radius=yes accounting=yes
            list(self._api(
                "/ppp/aaa/set",
                **{"use-radius": "yes", "accounting": "yes", "interim-update": interim_update},
            ))
        except Exception as exc:
            raise MikrotikError(f"تنظیم RADIUS روی میکروتیک ناموفق بود: {exc}") from exc

    # --------------------------------------------------------- certificates
    def ensure_self_signed_certificate(self, name: str, common_name: Optional[str] = None) -> str:
        """Returns the name of a signed certificate suitable for SSTP's
        server certificate, creating+self-signing one under `name` if none
        exists yet. Idempotent - if a certificate with this name already
        exists (e.g. from a previous push, or one the admin made by hand),
        it's reused as-is rather than recreated."""
        path = self._api.path("certificate")
        existing = list(path.select(Key("name")).where(Key("name") == name))
        if existing:
            return name
        try:
            path.add(name=name, **{"common-name": common_name or name, "key-usage": "tls-server"})
            # Self-sign it - RouterOS's /certificate sign is synchronous over
            # the API for a locally-held private key (no external CA involved).
            list(self._api("/certificate/sign", **{"number": name}))
        except Exception as exc:
            raise MikrotikError(f"ساخت گواهی برای SSTP ناموفق بود: {exc}") from exc
        return name

    # --------------------------------------------------------------- SSTP
    # SSTP wraps PPP inside a TLS tunnel, so (unlike plain OpenVPN/L2TP
    # username+password over RADIUS) it also needs a server certificate.
    # This only flips the SSTP server on and points it at a certificate +
    # RADIUS auth - it deliberately does NOT touch IP pools or PPP profiles,
    # same minimal-touch scope as push_radius_config above.
    def push_sstp_config(self, port: int, certificate_name: str = "usermanager-sstp") -> str:
        cert = self.ensure_self_signed_certificate(certificate_name)
        try:
            list(self._api(
                "/interface/sstp-server/server/set",
                enabled="yes",
                port=str(port),
                certificate=cert,
                authentication="mschap2",
            ))
        except Exception as exc:
            raise MikrotikError(f"تنظیم SSTP روی میکروتیک ناموفق بود: {exc}") from exc
        return cert

    # --------------------------------------------------------------- L2TP
    # Simple/classic L2TP-over-IPsec: a single shared pre-shared key secures
    # the IPsec layer for every client, then PPP negotiates over it with
    # RADIUS-authenticated per-user username/password (same as SSTP/OpenVPN
    # above). This is RouterOS's built-in `use-ipsec` shortcut on the L2TP
    # server itself - no separate /ip/ipsec peer/profile needed.
    def push_l2tp_config(self, ipsec_secret: str) -> None:
        try:
            list(self._api(
                "/interface/l2tp-server/server/set",
                enabled="yes",
                **{"use-ipsec": "yes", "ipsec-secret": ipsec_secret},
            ))
        except Exception as exc:
            raise MikrotikError(f"تنظیم L2TP/IPsec روی میکروتیک ناموفق بود: {exc}") from exc

    # -------------------------------------------------------------- IKEv2
    # RouterOS's "IKEv2" here is the same L2TP-server PPP/RADIUS stack as
    # push_l2tp_config, but with the IPsec layer negotiated explicitly as
    # IKEv2 (exchange-mode=ike2) via a dedicated /ip/ipsec peer+identity
    # instead of the server's own simplified ipsec-secret shortcut - so this
    # should NOT be combined with push_l2tp_config's ipsec-secret on the
    # same router (they configure the IPsec layer two different ways).
    # Per-user login is still RADIUS/PPP (same username+password as the
    # other protocols); the pre-shared key below only secures the IKE/IPsec
    # tunnel itself, matching this node's mt_ikev2_psk field.
    def push_ikev2_config(self, psk: str, peer_name: str = "usermanager-ikev2") -> None:
        try:
            profile_path = self._api.path("ip", "ipsec", "profile")
            if not list(profile_path.select(Key("name")).where(Key("name") == peer_name)):
                profile_path.add(name=peer_name, **{"dh-group": "modp2048", "enc-algorithm": "aes-256", "hash-algorithm": "sha256"})

            peer_path = self._api.path("ip", "ipsec", "peer")
            existing_peer = list(peer_path.select(Key(".id"), Key("name")).where(Key("name") == peer_name))
            peer_props = {"name": peer_name, "address": "0.0.0.0/0", "exchange-mode": "ike2", "passive": "yes", "profile": peer_name}
            if existing_peer:
                peer_path.update(**{".id": existing_peer[0][".id"]}, **peer_props)
            else:
                peer_path.add(**peer_props)

            identity_path = self._api.path("ip", "ipsec", "identity")
            existing_identity = list(identity_path.select(Key(".id"), Key("peer")).where(Key("peer") == peer_name))
            identity_props = {
                "peer": peer_name,
                "auth-method": "pre-shared-key",
                "secret": psk,
                "generate-policy": "port-strict",
                # RouterOS's default remote-id ("auto") tries to match the
                # client's declared identity (IDi) against a specific
                # expected value/type, which native iOS/Android/Windows
                # IKEv2 clients frequently fail (RouterOS logs "identity
                # not found for responder: FQDN:... peer: ADDR4:..." and
                # kills the SA right after phase 1, even though the PSK
                # itself is correct). "ignore" makes RouterOS accept any
                # identity the client presents and rely on the PSK alone
                # for authentication - this is the standard fix for
                # road-warrior PSK-based IKEv2 against RouterOS.
                "remote-id": "ignore",
            }
            if existing_identity:
                identity_path.update(**{".id": existing_identity[0][".id"]}, **identity_props)
            else:
                identity_path.add(**identity_props)

            # Same underlying L2TP/PPP/RADIUS server as push_l2tp_config -
            # use-ipsec=yes here just enables the IPsec layer in general;
            # the explicit peer above (exchange-mode=ike2) is what makes
            # RouterOS negotiate IKEv2 instead of the default IKEv1.
            list(self._api("/interface/l2tp-server/server/set", enabled="yes", **{"use-ipsec": "yes"}))
        except Exception as exc:
            raise MikrotikError(f"تنظیم IKEv2 روی میکروتیک ناموفق بود: {exc}") from exc

    # -------------------------------------------------------- import (read-only)
    def read_ppp_secrets(self) -> list[dict]:
        """Read-only listing of /ppp/secret entries already configured
        directly on the router (by the admin, outside the panel). Used only
        to import pre-existing accounts into the panel's database - the
        panel does not write to /ppp/secret at all otherwise (auth for
        panel-managed users goes through RADIUS instead)."""
        path = self._api.path("ppp", "secret")
        try:
            return list(path)
        except Exception as exc:
            raise MikrotikError(f"خواندن PPP secret های میکروتیک ناموفق بود: {exc}") from exc

    # ------------------------------------------- MikroTik's own User Manager
    # RouterOS has its own built-in User Manager (a separate RADIUS server,
    # /user-manager/...) that many admins already use to manage users with
    # quotas/expiry/simultaneous-session limits - independently of, and
    # before ever touching, /ppp/secret. It is a completely different data
    # source from /ppp/secret: User Manager accounts are protocol-agnostic
    # (the same username/password authenticates regardless of whether the
    # client connects via OpenVPN, L2TP, PPPoE, hotspot, etc. - User Manager
    # itself has no concept of "service"). All of these methods are
    # read-only and used only to import accounts into the panel.
    def read_um_users(self) -> list[dict]:
        try:
            return list(self._api.path("user-manager", "user"))
        except Exception as exc:
            raise MikrotikError(f"خواندن کاربران User Manager ناموفق بود: {exc}") from exc

    def read_um_profiles(self) -> list[dict]:
        """Reads /user-manager/profile - the profile DEFINITIONS themselves
        (name, starts-when, validity), as opposed to read_um_user_profiles()
        which reads the per-user ASSIGNMENTS. Needed because a user-profile
        assignment's own end-time is only computed by RouterOS once the
        profile has actually started (see read_um_user_profiles' docstring);
        for a profile with starts-when=first-auth that hasn't happened yet,
        this is the only place the intended validity duration is visible."""
        try:
            return list(self._api.path("user-manager", "profile"))
        except Exception as exc:
            raise MikrotikError(f"خواندن پروفایل‌های User Manager ناموفق بود: {exc}") from exc

    def read_um_user_profiles(self) -> list[dict]:
        """Reads /user-manager/user-profile - the assignment linking a user
        to a profile. Its end-time is a datetime RouterOS computes once the
        profile has actually started: immediately if the profile's
        starts-when=assigned, but only after the user's first successful
        authentication if starts-when=first-auth - before that first auth,
        end-time comes back empty/unknown even though the assignment's
        state is already "running" (ready to be used). See
        import_usermanager_accounts() in user_ops.py for how this is
        combined with read_um_profiles() to still capture an
        expire-days-after-first-use value for that case."""
        try:
            return list(self._api.path("user-manager", "user-profile"))
        except Exception as exc:
            raise MikrotikError(f"خواندن پروفایل‌های کاربران User Manager ناموفق بود: {exc}") from exc

    def read_um_profile_limitations(self) -> list[dict]:
        try:
            return list(self._api.path("user-manager", "profile-limitation"))
        except Exception as exc:
            raise MikrotikError(f"خواندن profile-limitation های User Manager ناموفق بود: {exc}") from exc

    def read_um_limitations(self) -> list[dict]:
        try:
            return list(self._api.path("user-manager", "limitation"))
        except Exception as exc:
            raise MikrotikError(f"خواندن limitation های User Manager ناموفق بود: {exc}") from exc

    def read_um_sessions(self) -> list[dict]:
        try:
            return list(self._api.path("user-manager", "session"))
        except Exception as exc:
            raise MikrotikError(f"خواندن session های User Manager ناموفق بود: {exc}") from exc

    def read_um_usage(self, user_ids: list[str]) -> dict[str, dict]:
        """Reads each user's TRUE lifetime total-download/total-upload via
        RouterOS's '/user-manager/user monitor' command - a live aggregate
        counter the router keeps per user, separate from (and far more
        complete than) /user-manager/session, which only retains a rolling
        window of recent sessions and badly undercounts anyone who has
        reconnected a few times. Confirmed against a live router: this
        matches exactly what Winbox's own User Manager > Users view shows.

        Batched in chunks (RouterOS accepts a comma-separated "numbers="
        list and returns one reply row per id, in the same order) to avoid
        one API round-trip per user on large installs.

        Returns {user_id (".id" string): {"total-download": int, ...}}."""
        if not user_ids:
            return {}
        result: dict[str, dict] = {}
        chunk_size = 200
        try:
            for i in range(0, len(user_ids), chunk_size):
                chunk = user_ids[i : i + chunk_size]
                rows = list(
                    self._api("/user-manager/user/monitor", **{"numbers": ",".join(chunk), "once": ""})
                )
                if len(rows) != len(chunk):
                    # RouterOS is documented/assumed to return exactly one
                    # reply row per requested id, in the same order - if
                    # that ever isn't true (a deleted user, a malformed
                    # row), positional zip() would silently misattribute
                    # usage between customers, which is billing/quota
                    # relevant (could wrongly cut a paying customer's
                    # service or under-charge another). Log loudly so a
                    # mismatch is at least visible instead of a silent
                    # misattribution.
                    logger.warning(
                        "user-manager/user/monitor returned %d rows for %d requested ids - "
                        "usage mapping for this chunk may be misaligned",
                        len(rows), len(chunk),
                    )
                for uid, row in zip(chunk, rows):
                    result[uid] = row
        except Exception as exc:
            raise MikrotikError(f"خواندن مصرف واقعی کاربران از User Manager ناموفق بود: {exc}") from exc
        return result
