"""Manages clients on a 3X-UI panel (github.com/MHSanaei/3x-ui) over its own
HTTP API - no SSH access to the Xray host needed at all.

Use this when Xray/3X-UI runs somewhere SSH can't reach (e.g. inside a
MikroTik container, as it does for at least one of our users). All that's
required is that the panel's web port is reachable from this server, plus
either an API token (Settings -> Authentication -> API Token in the panel -
preferred, skips the login form entirely) or the panel's admin
username/password, and the numeric id of the inbound to hand accounts out
on (visible in the 3X-UI inbound list/edit screen).

API notes: 3X-UI has gone through at least two different
client-management API shapes across its release history, and we don't know
in advance which one a given panel is running, so every client-management
call below tries the newer shape first and silently falls back to the
older one on failure:

  Auth (either works, tried in this order):
    - Authorization: Bearer <api_token>   (Settings -> Authentication -> API Token)
    - POST {base}/login {username, password} -> session cookie

  Reading (same on both API generations):
    GET {base}/panel/api/inbounds/get/:id -> inbound, including clientStats

  Newer client API (top-level /panel/api/clients, 3X-UI v3+):
    POST {base}/panel/api/clients/add            {..client fields.., inboundIds: [id]}
    POST {base}/panel/api/clients/update/:email   {..client fields..}
    POST {base}/panel/api/clients/del/:email

  Older/classic client API (nested under /panel/api/inbounds, most
  widely-deployed versions for years):
    POST {base}/panel/api/inbounds/addClient            {id, settings: json}
    POST {base}/panel/api/inbounds/updateClient/:uuid   {id, settings: json}
    POST {base}/panel/api/inbounds/:id/delClient/:uuid

{base} is the full base URL the admin configured, including any custom
"web base path" 3X-UI was installed with (e.g. http://1.2.3.4:2053/xyzpanel).

We only ever build vless-style client entries ({"id": uuid, "email":
email, "flow": flow}) - this matches the vless:// link our own
link_builder.py always generates, so the inbound on the 3X-UI side is
expected to be a vless inbound.
"""
from __future__ import annotations

import json
import uuid
from typing import Optional

import requests
import urllib3

from .xray_client import XrayError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ThreeXUIClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_id: int,
        api_token: str = "",
        timeout: int = 10,
    ):
        base_url = (base_url or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            # Users often paste just "host:port/path" - default to http so a
            # missing scheme doesn't crash with requests' "no connection
            # adapters" error instead of a clear message.
            base_url = f"http://{base_url}"
        self.base_url = base_url
        self.username = username
        self.password = password
        self.api_token = (api_token or "").strip()
        self.inbound_id = inbound_id
        self.timeout = timeout
        self.session = requests.Session()
        # Some panels sit behind a WAF/reverse proxy (Cloudflare, nginx +
        # basic bot filtering, etc.) that silently 403s requests carrying
        # the default "python-requests/x.x" User-Agent. Look like a normal
        # browser so those don't get in the way.
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        if self.api_token:
            self.session.headers["Authorization"] = f"Bearer {self.api_token}"
        self._logged_in = bool(self.api_token)  # token auth needs no login step

    # ------------------------------------------------------------------
    def connect(self):
        if not self.base_url or not self.inbound_id:
            raise XrayError("آدرس پنل 3X-UI یا شناسه اینباند تنظیم نشده است")
        if not self.api_token and not (self.username and self.password):
            raise XrayError("برای اتصال به پنل 3X-UI باید API Token یا یوزر/پسورد پنل را وارد کنید")
        if not self._logged_in:
            self._login()
        return self

    def close(self):
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------------
    def _send(self, method: str, url: str, **kwargs) -> dict:
        """POST/GET wrapper that turns "response wasn't JSON" into a
        diagnostic message (status code + a snippet of the actual body)
        instead of the opaque "Expecting value: line 1 column 1" from
        resp.json() - that error just means the panel sent back HTML/empty
        content instead of JSON, usually because the base URL's path (the
        "web base path" from 3X-UI's own settings) is wrong or the request
        got redirected to a login page."""
        try:
            resp = self.session.request(method, url, timeout=self.timeout, verify=False, **kwargs)
        except Exception as exc:
            raise XrayError(f"اتصال به پنل 3X-UI برقرار نشد: {exc}") from exc
        try:
            return resp.json()
        except ValueError:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            raise XrayError(
                f"پاسخ پنل 3X-UI به فرمت JSON نبود (کد HTTP {resp.status_code}). "
                f"احتمالا آدرس پنل یا مسیر امنیتی (web base path) اشتباه است. "
                f"شروع پاسخ دریافتی: «{snippet or 'خالی'}»"
            )

    def _login(self):
        data = self._send(
            "POST",
            f"{self.base_url}/login",
            json={"username": self.username, "password": self.password},
        )
        if not data.get("success"):
            raise XrayError(f"ورود به پنل 3X-UI ناموفق بود: {data.get('msg') or 'یوزر/پسورد اشتباه است'}")
        self._logged_in = True

    def _request(self, method: str, path: str, retry: bool = True, **kwargs) -> dict:
        if not self._logged_in:
            self._login()
        url = f"{self.base_url}{path}"
        data = self._send(method, url, **kwargs)

        if not data.get("success"):
            # session cookie may have expired - retry once after a fresh
            # login (only relevant for username/password auth; token auth
            # doesn't expire the same way, so don't loop on it).
            if retry and not self.api_token:
                self._logged_in = False
                return self._request(method, path, retry=False, **kwargs)
            raise XrayError(f"درخواست به پنل 3X-UI ناموفق بود: {data.get('msg') or 'خطای نامشخص'}")
        return data

    def _get(self, path: str) -> dict:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        return self._request("POST", path, json=body)

    def _try_post(self, path: str, body: dict) -> tuple[bool, dict]:
        """Like _post, but returns (ok, data/error-info) instead of raising -
        used to silently try the newer API shape before falling back to the
        older one."""
        try:
            return True, self._post(path, body)
        except XrayError as exc:
            return False, {"error": str(exc)}

    # ------------------------------------------------------------------
    def _get_inbound(self) -> dict:
        data = self._get(f"/panel/api/inbounds/get/{self.inbound_id}")
        return data.get("obj") or {}

    def test_connection(self):
        """Lightweight, side-effect-free check used by the "تست اتصال"
        button - logs in (or verifies the token) and confirms the
        configured inbound id exists."""
        obj = self._get_inbound()
        if not obj:
            raise XrayError("اینباند با این شناسه در پنل 3X-UI پیدا نشد")

    def get_link_settings(self) -> dict:
        """Reads the real host/port/network/security/sni the configured
        inbound is actually listening on/with, straight from 3X-UI, so the
        panel can auto-fill Node.xr_public_* instead of relying on
        hand-entered values (which default to port 443 + tls and silently
        produce a broken vless:// link - no host, wrong port/security - if
        the admin forgets to fill them in). Host is derived from this
        client's own base_url (3X-UI's admin API doesn't expose its own
        public domain), port/network/security/sni come from the inbound's
        `port` and `streamSettings`. Best-effort: returns {} if the inbound
        can't be read, and leaves out anything it can't parse."""
        obj = self._get_inbound()
        if not obj:
            return {}
        from urllib.parse import urlparse
        info: dict = {}
        host = urlparse(self.base_url).hostname
        if host:
            info["host"] = host
        port = obj.get("port")
        if port:
            info["port"] = int(port)
        try:
            stream = json.loads(obj.get("streamSettings") or "{}")
        except (ValueError, TypeError):
            stream = {}
        network = stream.get("network") or "tcp"
        security = stream.get("security") or "none"
        info["network"] = network
        info["security"] = security
        sni = ""
        if security == "tls":
            tls = stream.get("tlsSettings") or {}
            sni = tls.get("serverName") or ""
        elif security == "reality":
            reality = stream.get("realitySettings") or {}
            names = reality.get("serverNames") or []
            sni = names[0] if names else ""
        info["sni"] = sni
        return info

    def get_online_emails(self) -> set[str]:
        """Returns the set of client emails 3X-UI currently considers
        online - the same live signal that powers the "online" count shown
        on 3X-UI's own inbound list (derived from Xray's per-user
        connection stats, refreshed by the panel itself). Best-effort: on
        any failure returns an empty set rather than raising, since a
        transient miss here shouldn't break the traffic-sync poll cycle
        that calls it."""
        try:
            data = self._post("/panel/api/inbounds/onlines", {})
        except XrayError:
            return set()
        obj = data.get("obj")
        if not obj:
            return set()
        return set(obj)

    def _get_client_traffic(self, email: str) -> dict:
        """Per-client traffic (up/down) isn't reliably present on the
        inbound object itself (its `clientStats` came back null on at least
        one real panel we tested against) - it has to be read per-email
        from a dedicated endpoint instead. Tries the newer top-level
        endpoint first, then the classic one (confirmed working via a live
        test: /panel/api/inbounds/getClientTraffics/:email returns
        {"up":.., "down":.., "allTime":.., ...})."""
        for path in (
            f"/panel/api/clients/traffic/{email}",
            f"/panel/api/inbounds/getClientTraffics/{email}",
        ):
            try:
                data = self._get(path)
            except XrayError:
                continue
            obj = data.get("obj")
            if obj:
                return obj
        return {}

    def list_clients_with_usage(self) -> list[dict]:
        """Returns every client already configured on the target inbound
        (e.g. created directly in 3X-UI before this node was connected to
        the panel), combined with its usage counters - used by the "ایمپورت
        از 3X-UI" flow. Read-only; nothing is changed on the panel."""
        obj = self._get_inbound()
        try:
            clients = json.loads(obj.get("settings") or "{}").get("clients", [])
        except (ValueError, TypeError):
            clients = []

        result = []
        for c in clients:
            email = c.get("email")
            if not email:
                continue
            traffic = self._get_client_traffic(email)
            result.append({
                "id": c.get("id") or c.get("password"),  # vless/vmess use id, trojan uses password
                "email": email,
                "flow": c.get("flow") or "",
                "enable": c.get("enable", True),
                "up": int(traffic.get("up", 0) or 0),
                "down": int(traffic.get("down", 0) or 0),
                # Despite the field's name, 3X-UI stores this in raw bytes,
                # not gigabytes (confirmed live: 21474836480 == exactly 20
                # GiB) - 0 means unlimited.
                "totalGB": int(c.get("totalGB", 0) or 0),
                # Unix epoch milliseconds, 0 = never expires.
                "expiryTime": int(c.get("expiryTime", 0) or 0),
            })
        return result

    # ------------------------------------------------------------------
    @staticmethod
    def _client_fields(email: str, client_uuid: str, flow: str, enabled: bool = True) -> dict:
        return {
            "id": client_uuid,
            "email": email,
            "flow": flow or "",
            "limitIp": 0,
            "totalGB": 0,
            "expiryTime": 0,
            "enable": enabled,
            "tgId": 0,
            "subId": "",
            "reset": 0,
        }

    def add_client(
        self,
        inbound_tag: str,  # unused - kept for interface parity with XrayClient
        email: str,
        client_uuid: Optional[str] = None,
        flow: str = "",
    ) -> str:
        """Adds a vless client to the configured inbound. Returns the uuid
        used. totalGB/expiryTime are left at 0 (unlimited) - this panel
        already enforces quota/expiry itself via enabling/disabling the
        connection, same as the SSH-managed path."""
        client_uuid = client_uuid or str(uuid.uuid4())
        fields = self._client_fields(email, client_uuid, flow)

        # try the newer top-level /clients API first
        ok, _ = self._try_post("/panel/api/clients/add", {**fields, "inboundIds": [self.inbound_id]})
        if ok:
            return client_uuid

        # fall back to the classic /inbounds/addClient shape
        self._post(
            "/panel/api/inbounds/addClient",
            {"id": self.inbound_id, "settings": json.dumps({"clients": [fields]})},
        )
        return client_uuid

    def remove_client(self, inbound_tag: str, email: str, client_uuid: Optional[str] = None):
        # try the newer top-level /clients API first (keyed by email)
        ok, _ = self._try_post(f"/panel/api/clients/del/{email}", {})
        if ok:
            return

        # fall back to the classic delClient endpoint, which needs the uuid
        if not client_uuid:
            obj = self._get_inbound()
            try:
                clients = json.loads(obj.get("settings") or "{}").get("clients", [])
            except (ValueError, TypeError):
                clients = []
            match = next((c for c in clients if c.get("email") == email), None)
            client_uuid = match.get("id") if match else None
        if not client_uuid:
            return  # already gone
        self._post(f"/panel/api/inbounds/{self.inbound_id}/delClient/{client_uuid}", {})

    def set_client_enabled(self, inbound_tag: str, email: str, uuid_: str, flow: str, enabled: bool):
        # newer API: update the existing client's `enable` flag in place
        fields = self._client_fields(email, uuid_, flow, enabled=enabled)
        ok, _ = self._try_post(f"/panel/api/clients/update/{email}", fields)
        if ok:
            return

        # classic API has no per-client enable flag over this route - mirror
        # the SSH client's approach of adding/removing the whole entry
        if enabled:
            self.add_client(inbound_tag, email, uuid_, flow)
        else:
            self.remove_client(inbound_tag, email, uuid_)

    # ------------------------------------------------------------------
    def query_all_user_stats(self) -> dict[str, dict[str, int]]:
        """Returns {email: {"uplink": n, "downlink": n}} - cumulative totals
        since the panel last reset that client's counter (read per-email,
        see _get_client_traffic - clientStats on the inbound object itself
        isn't reliable across panel versions). Callers diff against
        previously stored values."""
        obj = self._get_inbound()
        try:
            clients = json.loads(obj.get("settings") or "{}").get("clients", [])
        except (ValueError, TypeError):
            clients = []

        results: dict[str, dict[str, int]] = {}
        for c in clients:
            email = c.get("email")
            if not email:
                continue
            traffic = self._get_client_traffic(email)
            results[email] = {
                "uplink": int(traffic.get("up", 0) or 0),
                "downlink": int(traffic.get("down", 0) or 0),
            }
        return results
