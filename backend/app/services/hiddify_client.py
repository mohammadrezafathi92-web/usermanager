"""Client for Hiddify Manager's admin REST API (v2).

Verified directly against the panel's own source (hiddify/hiddifypanel on
GitHub - panel/commercial/restapi/v2/admin/{user_api,users_api,schema}.py,
models/user.py, auth.py) since the public docs page only documents the base
paths and auth header, not the concrete endpoint/field shapes.

Key differences from XrayClient/ThreeXUIClient/MarzbanClient that shape this
client's design:

- Auth is a single static header, no login call: ``Hiddify-API-Key: <admin's
  own uuid>`` (see hiddifypanel/auth.py - ``get_account_by_api_key`` treats
  the key as literally the AdminUser's uuid). ``node.xr_panel_api_token`` is
  reused to store this key (same field 3X-UI's optional API-token login
  uses) - no new column needed.
- There is no per-inbound "email" concept at all. A Hiddify "user" is one
  row identified by its own ``uuid``, simultaneously active on every
  protocol the panel has configured (vless, vmess, trojan, wireguard, ...).
  ``inbound_tag`` parameters on this class are accepted for interface
  parity with the other clients but are never used.
- ``uuid`` doubles as the login credential AND the config-access key -
  there's no separate "client_uuid on inbound X" like 3X-UI/Marzban, so
  ``client_uuid`` here is just Hiddify's own ``uuid`` field.
- Quota/expiry: ``usage_limit_GB`` (float, GB) and ``package_days`` (int)
  with an optional ``start_date``. Leaving ``start_date`` unset makes
  ``remaining_days`` a constant equal to ``package_days`` (never counts
  down - see User.remaining_days), so a large ``package_days`` plus no
  ``start_date`` is this panel's equivalent of "never expires". Likewise a
  very large ``usage_limit_GB`` (capped internally at 1_000_000 GB) stands
  in for "unlimited" - this app enforces the real quota/expiry itself
  (quota_manager.py), the panel-side numbers only need to never be the
  actual limiting factor.
- ``current_usage_GB`` is a single cumulative counter (no up/down split),
  same simplification as MarzbanClient: reported entirely as "downlink"
  with "uplink": 0. It won't be silently reset by the panel because every
  user is created with ``mode: "no_reset"``.
- No bulk "who's currently online" endpoint is exposed via this API (same
  situation as Marzban) - get_online_emails() returns an empty set.
- No ``get_link_settings()`` is implemented on purpose: Hiddify has no
  single host/port/network/security/sni to sync the way 3X-UI/Marzban do
  (one user = many protocols at once). ``client_for_node()``/
  ``routers/nodes.py`` already skip that sync step via ``hasattr``, so
  omitting the method is enough to opt out cleanly. Building a customer's
  actual link is a Hiddify-specific "subscription URL"
  (``{sub_base}/{uuid}/``), handled separately in
  ``user_ops.get_connection_share()`` rather than through
  ``link_builder.build_vless_link`` - see the ``xr_public_host`` reuse note
  there.
"""

from urllib.parse import urljoin

import requests

from .xray_client import XrayError

ONE_GIG = 1024 * 1024 * 1024


class HiddifyClient:
    def __init__(self, base_url, api_key, timeout=10):
        self.base_url = (base_url or "").strip().rstrip("/")
        if self.base_url and not self.base_url.startswith(("http://", "https://")):
            self.base_url = "http://" + self.base_url
        self.api_key = (api_key or "").strip()
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        )
        self._connected = False

    def connect(self):
        if not self.base_url:
            raise XrayError("آدرس پنل Hiddify تنظیم نشده است")
        if not self.api_key:
            raise XrayError("کلید API پنل Hiddify (Hiddify-API-Key) تنظیم نشده است")
        self.session.headers["Hiddify-API-Key"] = self.api_key
        self._connected = True
        return self

    def close(self):
        self._connected = False
        try:
            self.session.close()
        except Exception:
            pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _url(self, path):
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _send(self, method, url, **kwargs):
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise XrayError(f"خطا در ارتباط با پنل Hiddify: {exc}") from exc
        if not resp.content:
            return resp.status_code, {}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, {}

    def _get(self, path, **kwargs):
        return self._send("GET", self._url(path), **kwargs)

    def _post(self, path, json_body):
        status, data = self._send("POST", self._url(path), json=json_body)
        if not (200 <= status < 300):
            raise XrayError(f"خطا در ایجاد کاربر Hiddify: {data.get('message') or data}")
        return data

    def _patch(self, path, json_body):
        status, data = self._send("PATCH", self._url(path), json=json_body)
        if not (200 <= status < 300):
            raise XrayError(f"خطا در به‌روزرسانی کاربر Hiddify: {data.get('message') or data}")
        return data

    def _delete(self, path):
        status, data = self._send("DELETE", self._url(path))
        if status == 404:
            return  # already gone - not an error
        if not (200 <= status < 300):
            raise XrayError(f"خطا در حذف کاربر Hiddify: {data.get('message') or data}")

    def test_connection(self):
        status, data = self._get("/api/v2/admin/me/")
        if status != 200:
            raise XrayError(
                f"اتصال به پنل Hiddify ناموفق بود (کد {status}) - آدرس و کلید API را بررسی کنید"
            )

    def _list_users(self):
        status, data = self._get("/api/v2/admin/user/")
        if status == 404:
            return []  # "You have no user" - abort(404) on an empty list
        if status != 200:
            raise XrayError(f"خطا در دریافت لیست کاربران Hiddify (کد {status})")
        return data if isinstance(data, list) else []

    def list_client_emails(self, inbound_tag):
        return [u.get("name") for u in self._list_users() if u.get("name")]

    def get_online_emails(self):
        return set()

    def list_clients_with_usage(self):
        out = []
        for u in self._list_users():
            usage_gb = u.get("current_usage_GB") or 0
            out.append({
                "id": u.get("uuid"),
                "email": u.get("name"),
                "flow": "",
                "enable": bool(u.get("enable")),
                "up": 0,
                "down": int(usage_gb * ONE_GIG),
                "totalGB": u.get("usage_limit_GB"),
                "expiryTime": 0,
            })
        return out

    def add_client(self, inbound_tag, email, client_uuid=None, flow=""):
        body = {
            "name": email,
            "enable": True,
            "usage_limit_GB": 1_000_000,
            "package_days": 10_000,
            "mode": "no_reset",
        }
        if client_uuid:
            body["uuid"] = client_uuid
        try:
            data = self._post("/api/v2/admin/user/", body)
        except XrayError:
            if not client_uuid:
                raise
            # Most likely "the user exists" (re-provisioning an already
            # existing uuid) - fall back to an update-in-place, same
            # POST-then-PUT/PATCH fallback pattern as MarzbanClient.
            data = self._patch(f"/api/v2/admin/user/{client_uuid}/", body)
        return data.get("uuid") or client_uuid

    def remove_client(self, inbound_tag, email, client_uuid=None):
        target_uuid = client_uuid
        if not target_uuid:
            for u in self._list_users():
                if u.get("name") == email:
                    target_uuid = u.get("uuid")
                    break
        if not target_uuid:
            return  # nothing to delete - already gone
        self._delete(f"/api/v2/admin/user/{target_uuid}/")

    def set_client_enabled(self, inbound_tag, email, uuid_, flow, enabled):
        if not uuid_:
            return False
        try:
            self._patch(f"/api/v2/admin/user/{uuid_}/", {"enable": bool(enabled)})
            return True
        except XrayError:
            if not enabled:
                raise
            # Self-heals a client that vanished from the panel, same as
            # MarzbanClient.set_client_enabled.
            self.add_client(inbound_tag, email, client_uuid=uuid_, flow=flow)
            return True

    def query_all_user_stats(self):
        stats = {}
        for u in self._list_users():
            name = u.get("name")
            if not name:
                continue
            usage_gb = u.get("current_usage_GB") or 0
            stats[name] = {"uplink": 0, "downlink": int(usage_gb * ONE_GIG)}
        return stats
