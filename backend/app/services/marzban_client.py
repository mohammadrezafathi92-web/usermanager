"""Manages clients on a Marzban panel (github.com/Gozargah/Marzban) over its
own HTTP API - no SSH access to the Xray host needed at all. Same idea as
threexui_client.py (see that module's docstring for why a panel-API client
exists alongside the SSH-managed one), just for the second most common
Xray panel after 3X-UI.

Marzban's data model differs from 3X-UI's in one important way: a 3X-UI
client belongs to exactly ONE inbound (Node.xr_panel_inbound_id, an int),
while a Marzban user is created independent of any inbound and then
assigned a set of inbound TAGS per protocol ({"vless": ["tag1", "tag2"]}).
We only ever need one inbound per node (same as every other panel mode
here), so Node.xr_inbound_tag (already used as the SSH-managed config.json
tag) doubles as "which Marzban inbound tag this node's clients get
assigned to" - no new Node columns needed.

We only ever build vless clients, matching link_builder.py's
build_vless_link (the only xray link type this panel ever generates).

API notes (github.com/Gozargah/Marzban, app/routers/{admin,user,system}.py):

  Auth:
    POST {base}/api/admin/token  (form-encoded: username, password)
    -> {"access_token": "...", "token_type": "bearer"}
    Authorization: Bearer <access_token> on every other request.

  Reading:
    GET {base}/api/inbounds -> {protocol: [{tag, protocol, network, tls, port}]}
    GET {base}/api/users?offset=&limit= -> {"users": [...], "total": n}
    GET {base}/api/user/{username} -> single user, same shape as above
    GET {base}/api/hosts -> {tag: [{sni, security, ...}]} (sudo admin only -
      best-effort, used only to fill in sni; skipped silently for a
      non-sudo admin account)

  Writing:
    POST {base}/api/user     {username, proxies, inbounds, data_limit,
                               expire, status}
    PUT  {base}/api/user/{username}   (partial update - only fields
                               actually included in the body are changed;
                               omitted fields keep their existing value)
    DELETE {base}/api/user/{username}

  proxies is keyed by protocol: {"vless": {"id": <uuid>, "flow": <flow>}}.
  data_limit is bytes (0 = unlimited, matches our own convention). expire
  is a unix epoch **seconds** timestamp (0/None = never expires) - note
  this is SECONDS, unlike 3X-UI's expiryTime which is milliseconds.

  used_traffic on a user is a single cumulative byte counter - Marzban
  doesn't split it into uplink/downlink the way 3X-UI's clientStats does.
  query_all_user_stats() below reports the whole delta as "downlink" (see
  its docstring) - this only affects the up/down split shown on usage
  charts, never the combined total actually enforced against quota (see
  quota_manager.py's _apply_delta, which sums both directions anyway).
"""
from __future__ import annotations

import uuid
from typing import Optional

import requests
import urllib3

from .xray_client import XrayError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class MarzbanClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        inbound_tag: str,
        timeout: int = 10,
    ):
        base_url = (base_url or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        self.base_url = base_url
        self.username = username
        self.password = password
        self.inbound_tag = inbound_tag
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
        })
        self._logged_in = False
        # Set once /api/hosts is confirmed to 403 (non-sudo admin account) so
        # get_link_settings doesn't keep retrying (and logging) it forever.
        self._hosts_forbidden = False

    # ------------------------------------------------------------------
    def connect(self):
        if not self.base_url or not self.inbound_tag:
            raise XrayError("آدرس پنل Marzban یا تگ اینباند تنظیم نشده است")
        if not (self.username and self.password):
            raise XrayError("برای اتصال به پنل Marzban باید یوزر/پسورد ادمین را وارد کنید")
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
    def _send(self, method: str, url: str, **kwargs) -> tuple[int, dict]:
        """Same "turn a bad response into a diagnostic message" idea as
        ThreeXUIClient._send - returns (status_code, json-or-{}) so callers
        can tell a real 404 (e.g. "user not found", which some callers
        below tolerate) apart from a connection/parsing failure."""
        try:
            resp = self.session.request(method, url, timeout=self.timeout, verify=False, **kwargs)
        except Exception as exc:
            raise XrayError(f"اتصال به پنل Marzban برقرار نشد: {exc}") from exc
        if resp.status_code == 204 or not resp.content:
            return resp.status_code, {}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            raise XrayError(
                f"پاسخ پنل Marzban به فرمت JSON نبود (کد HTTP {resp.status_code}). "
                f"شروع پاسخ دریافتی: «{snippet or 'خالی'}»"
            )

    def _login(self):
        status, data = self._send(
            "POST",
            f"{self.base_url}/api/admin/token",
            data={"username": self.username, "password": self.password},
        )
        token = data.get("access_token")
        if status != 200 or not token:
            raise XrayError(f"ورود به پنل Marzban ناموفق بود: {data.get('detail') or 'یوزر/پسورد اشتباه است'}")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._logged_in = True

    def _request(self, method: str, path: str, retry: bool = True, **kwargs) -> tuple[int, dict]:
        if not self._logged_in:
            self._login()
        status, data = self._send(method, f"{self.base_url}{path}", **kwargs)
        if status == 401 and retry:
            # Token expired/invalid - log in again once before giving up.
            self._logged_in = False
            self._login()
            return self._request(method, path, retry=False, **kwargs)
        return status, data

    def _get(self, path: str) -> tuple[int, dict]:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        status, data = self._request("POST", path, json=body)
        if status not in (200, 201):
            raise XrayError(f"درخواست به پنل Marzban ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")
        return data

    def _put(self, path: str, body: dict) -> dict:
        status, data = self._request("PUT", path, json=body)
        if status != 200:
            raise XrayError(f"درخواست به پنل Marzban ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")
        return data

    # ------------------------------------------------------------------
    def test_connection(self):
        """Lightweight, side-effect-free check used by the "تست اتصال"
        button - logs in and confirms the configured inbound tag exists
        under the vless protocol."""
        status, data = self._get("/api/inbounds")
        if status != 200:
            raise XrayError(f"خواندن لیست اینباندهای پنل Marzban ناموفق بود (کد HTTP {status})")
        vless_tags = {ib.get("tag") for ib in (data.get("vless") or [])}
        if self.inbound_tag not in vless_tags:
            raise XrayError(f"اینباند vless با تگ «{self.inbound_tag}» در پنل Marzban پیدا نشد")

    def list_client_emails(self, inbound_tag: str) -> list[str]:
        """Every username on this panel whose vless inbounds include ours -
        used by scripts/report_orphan_connections.py. inbound_tag param
        kept for interface parity with XrayClient/ThreeXUIClient (this
        client is already scoped to self.inbound_tag)."""
        return [u["username"] for u in self._list_users_for_tag()]

    def get_link_settings(self) -> dict:
        """Best-effort host/port/network/security/sni for the configured
        inbound tag - same purpose as ThreeXUIClient.get_link_settings (see
        its docstring). sni comes from /api/hosts, which only a sudo admin
        account can read - silently left blank for a non-sudo one rather
        than failing the whole lookup."""
        status, data = self._get("/api/inbounds")
        if status != 200:
            return {}
        inbound = next((ib for ib in (data.get("vless") or []) if ib.get("tag") == self.inbound_tag), None)
        if not inbound:
            return {}
        from urllib.parse import urlparse
        info: dict = {}
        host = urlparse(self.base_url).hostname
        if host:
            info["host"] = host
        port = inbound.get("port")
        if port:
            try:
                info["port"] = int(port)
            except (TypeError, ValueError):
                pass
        info["network"] = inbound.get("network") or "tcp"
        tls = inbound.get("tls") or "none"
        info["security"] = "tls" if tls in ("tls", "reality") else "none"

        if not self._hosts_forbidden:
            status, hosts = self._get("/api/hosts")
            if status == 403:
                self._hosts_forbidden = True
            elif status == 200:
                entries = hosts.get(self.inbound_tag) or []
                sni = next((h.get("sni") for h in entries if h.get("sni")), None)
                if sni:
                    info["sni"] = sni
        return info

    def get_online_emails(self) -> set[str]:
        """Marzban has no single "who's online right now" endpoint the way
        3X-UI's /panel/api/inbounds/onlines does - each user's own
        `online_at` timestamp is the closest equivalent, but reading it for
        everyone means listing every user anyway. Not worth the extra
        request on every poll cycle just for an "online" badge; returns an
        empty set like ThreeXUIClient does on failure (best-effort,
        callers already treat this as "unknown, not necessarily offline")."""
        return set()

    def _list_users_for_tag(self, page_size: int = 200) -> list[dict]:
        """All Marzban users assigned to self.inbound_tag under vless -
        Marzban has no server-side "users of this inbound" filter, so this
        pages through every user on the panel and filters client-side."""
        out: list[dict] = []
        offset = 0
        while True:
            status, data = self._get(f"/api/users?offset={offset}&limit={page_size}")
            if status != 200:
                break
            users = data.get("users") or []
            for u in users:
                if self.inbound_tag in (u.get("inbounds", {}).get("vless") or []):
                    out.append(u)
            if len(users) < page_size:
                break
            offset += page_size
        return out

    def list_clients_with_usage(self) -> list[dict]:
        """Returns every client already configured on this panel for our
        inbound tag, combined with usage - used by the "ایمپورت از پنل"
        flow and by rebuild-clients' "already there" check. Read-only."""
        result = []
        for u in self._list_users_for_tag():
            vless = (u.get("proxies") or {}).get("vless") or {}
            expire = u.get("expire") or 0
            result.append({
                "id": vless.get("id"),
                "email": u.get("username"),
                "flow": vless.get("flow") or "",
                "enable": u.get("status") == "active",
                "up": 0,
                "down": int(u.get("used_traffic", 0) or 0),
                "totalGB": int(u.get("data_limit", 0) or 0),
                # Marzban's expire is epoch SECONDS (3X-UI uses ms) -
                # normalize to ms here so callers (import_threexui_clients-
                # style code) can keep treating this shape uniformly.
                "expiryTime": int(expire) * 1000 if expire else 0,
            })
        return result

    # ------------------------------------------------------------------
    def add_client(
        self,
        inbound_tag: str,  # unused - kept for interface parity with XrayClient/ThreeXUIClient
        email: str,
        client_uuid: Optional[str] = None,
        flow: str = "",
    ) -> str:
        """Creates a Marzban user with one vless proxy on the configured
        inbound tag. Returns the uuid used. data_limit/expire are left at 0
        (unlimited) - this panel is only used to host the client, quota/
        expiry enforcement stays entirely on this panel's side (same as
        every other xray panel mode)."""
        client_uuid = client_uuid or str(uuid.uuid4())
        body = {
            "username": email,
            "proxies": {"vless": {"id": client_uuid, "flow": flow or ""}},
            "inbounds": {"vless": [self.inbound_tag]},
            "data_limit": 0,
            "expire": 0,
            "status": "active",
        }
        try:
            self._post("/api/user", body)
        except XrayError:
            # Most likely "user already exists" (e.g. a retried request, or
            # the username collides with something created directly on the
            # panel) - fall through to updating it in place instead of
            # leaving the caller with a client that silently doesn't exist.
            self._put(f"/api/user/{email}", body)
        return client_uuid

    def remove_client(self, inbound_tag: str, email: str, client_uuid: Optional[str] = None):
        status, data = self._request("DELETE", f"/api/user/{email}")
        if status not in (200, 204, 404):
            raise XrayError(f"حذف کلاینت از پنل Marzban ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")
        # 404 == already gone, same tolerance as ThreeXUIClient.remove_client.

    def set_client_enabled(self, inbound_tag: str, email: str, uuid_: str, flow: str, enabled: bool) -> bool:
        """Toggles status active/disabled WITHOUT deleting the user - PUT
        only sends `status`, so Marzban's own partial-update semantics
        leave proxies/inbounds/data_limit untouched (see this module's
        docstring). Self-heals by (re)creating the client if it's missing
        and the caller wants it enabled - same policy as
        ThreeXUIClient.set_client_enabled, for the same reason (a quota-cut
        client must come back correctly on renewal even if it was removed
        by an older code path or a manual panel edit)."""
        status, data = self._request("PUT", f"/api/user/{email}", json={"status": "active" if enabled else "disabled"})
        if status == 200:
            return True
        if status == 404 and enabled:
            self.add_client(inbound_tag, email, uuid_, flow)
            return True
        return False

    # ------------------------------------------------------------------
    def query_all_user_stats(self) -> dict[str, dict[str, int]]:
        """Returns {email: {"uplink": 0, "downlink": n}} - see this
        module's docstring for why Marzban's single cumulative
        `used_traffic` counter is reported entirely as "downlink" (the
        combined total is what actually matters for quota enforcement;
        only the up/down chart split is affected)."""
        results: dict[str, dict[str, int]] = {}
        for u in self._list_users_for_tag():
            username = u.get("username")
            if not username:
                continue
            results[username] = {"uplink": 0, "downlink": int(u.get("used_traffic", 0) or 0)}
        return results
