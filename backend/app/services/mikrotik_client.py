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

    # ------------------------------------------------------------- RADIUS
    # OpenVPN/L2TP users are now authenticated via RADIUS (this panel runs
    # its own RADIUS server - see app/services/radius_server.py) instead of
    # local PPP secrets. The panel does NOT touch the OpenVPN/L2TP server,
    # certificates or IPsec config on the router - those remain fully
    # manual. The one exception, added at the admin's explicit request, is
    # this helper: it registers the panel as a `/radius` client on the
    # router and flips `ppp aaa` to use it, so the admin doesn't have to
    # type those two RouterOS commands by hand.
    def push_radius_config(
        self,
        panel_host: str,
        secret: str,
        auth_port: int,
        acct_port: int,
        service: str = "ppp",
        interim_update: str = "00:05:00",
    ) -> None:
        path = self._api.path("radius")
        try:
            existing = list(
                path.select(Key(".id"), Key("address"), Key("service")).where(Key("address") == panel_host)
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
            # /ppp/aaa is a singleton settings menu (no .id) - "set" applies
            # directly, equivalent to: /ppp aaa set use-radius=yes accounting=yes
            list(self._api(
                "/ppp/aaa/set",
                **{"use-radius": "yes", "accounting": "yes", "interim-update": interim_update},
            ))
        except Exception as exc:
            raise MikrotikError(f"تنظیم RADIUS روی میکروتیک ناموفق بود: {exc}") from exc

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
