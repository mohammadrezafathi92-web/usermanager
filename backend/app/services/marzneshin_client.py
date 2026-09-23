"""Manages clients on a Marzneshin panel (github.com/marzneshin/marzneshin)
over its own HTTP API - no SSH access to the Xray host needed at all. Same
idea as marzban_client.py (it's a fork of Marzban), but its data model
diverges enough that it needs its own client rather than reusing that one.

Verified directly from the panel's own source on GitHub (marzneshin/
marzneshin, app/routes/{admin,user,service}.py, app/models/user.py,
app/db/models.py) since the public docs don't cover the REST admin API in
enough detail:

  Auth:
    POST {base}/api/admins/token  (form-encoded: username, password)
    -> {"access_token": "...", "token_type": "bearer", "is_sudo": bool}
    Authorization: Bearer <access_token> on every other request.

  Users are NOT scoped to a single inbound/node the way 3X-UI/Marzban
  clients are. A Marzneshin user is assigned a list of "service" IDs
  (app/models/service.py) - a service is an admin-defined group of
  inbounds, usually spanning one or more nodes. We only ever need one
  inbound/service per node here (same limitation as every other panel
  mode), so Node.xr_panel_inbound_id (already an int column, otherwise
  used for 3X-UI's numeric inbound id) doubles as "which Marzneshin
  service this node's clients get assigned to" - the admin creates that
  service on the Marzneshin panel ahead of time and pastes its id here,
  same idea as pasting an inbound tag for Marzban.

  Bigger mismatch: Marzneshin never lets the API caller choose a client's
  per-protocol uuid/password. Every user has one server-generated `key`
  (a free-form string, panel default: random 32-char hex - but it's a
  plain request field with just a default, so we CAN supply our own), and
  vmess/vless uuid + trojan/shadowsocks password are derived from that key
  by the node agent. There's no per-protocol identifier exposed over this
  API at all - only a ready-made subscription URL
  (`{prefix}/sub/{username}/{key}`, see app/db/models.py's
  User.subscription_url). So, like HiddifyClient, this integration hands
  the customer that subscription URL instead of a single vless:// link -
  see get_link_settings's deliberate omission below and user_ops.py's
  marzneshin branch of get_connection_share().

  We choose the `key` ourselves at creation time (reusing the same
  client_uuid every other panel mode generates) specifically so that
  get_connection_share() can rebuild `{prefix}/sub/{username}/{key}`
  locally from stored Connection fields, without an extra live API call
  on every share-link view - `prefix` is Node.xr_public_host if the admin
  filled it in (same repurposing as HiddifyClient; safe because this
  class also has no get_link_settings() for routers/nodes.py to sync it
  from), else Node.xr_panel_base_url as a same-domain-panel default.

  Username charset mismatch: Marzneshin usernames must match ^\\w{3,32}$
  (letters/digits/underscore only, lowercased), but this app's generated
  `email` values look like "cust1ab12@usermanager.local" - not valid here.
  sanitize_username() below strips/truncates to something that fits. To
  keep the add_client/remove_client/set_client_enabled/list_* interface
  working in terms of the *original* email (as every other panel mode
  does, and as scripts/report_orphan_connections.py relies on when
  comparing against Connection.xr_email), the untouched email is stashed
  in the user's free-text `note` field at creation and read back from it
  everywhere a client dict/email list is returned; sanitize_username() is
  only used to compute the URL path segment / username field itself.

  Reading:
    GET {base}/api/services/{id} -> confirms the configured service
      exists (test_connection).
    GET {base}/api/users?page=&size= -> fastapi-pagination Page:
      {"items": [...], "total", "page", "size", "pages"}. No per-service
      filter server-side, so (like MarzbanClient) this pages through every
      user on the panel and filters client-side on `service_ids`.

  Writing:
    POST {base}/api/users        {username, key, service_ids,
                                    expire_strategy, data_limit,
                                    data_limit_reset_strategy, note}
    PUT  {base}/api/users/{username}   (same shape - modify_user's
                                    pydantic model still expects the
                                    "required" User fields resent, so the
                                    exact same body used for POST is
                                    reused here rather than a true partial
                                    update)
    DELETE {base}/api/users/{username}
    POST {base}/api/users/{username}/enable
    POST {base}/api/users/{username}/disable

  expire_strategy is always "never" and data_limit always 0 (unlimited) -
  quota/expiry enforcement stays entirely on this app's side, same as
  every other xray panel mode.

  used_traffic on a user is a single cumulative byte counter, same
  situation as Marzban's used_traffic - query_all_user_stats() reports it
  all as "downlink" (see marzban_client.py's docstring for why this only
  affects the up/down chart split, never the combined total enforced
  against quota).
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

import requests
import urllib3

from .xray_client import XrayError

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_USERNAME_BAD_CHARS = re.compile(r"[^a-z0-9_]")


def sanitize_username(raw: str) -> str:
    """Turns this app's generated email ("cust1ab12@usermanager.local")
    into something matching Marzneshin's ^\\w{3,32}$ username rule.
    Deterministic (same input always yields the same output) so
    get_connection_share() can recompute it from Connection.xr_email
    without any extra storage or API call."""
    cleaned = _USERNAME_BAD_CHARS.sub("", (raw or "").lower())
    cleaned = cleaned[:32]
    if len(cleaned) < 3:
        cleaned = (cleaned + "usr")[:3]
    return cleaned


class MarzneshinClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        service_id: Optional[int],
        timeout: int = 10,
    ):
        base_url = (base_url or "").strip().rstrip("/")
        if base_url and not base_url.startswith(("http://", "https://")):
            base_url = f"http://{base_url}"
        self.base_url = base_url
        self.username = username
        self.password = password
        try:
            self.service_id = int(service_id) if service_id else None
        except (TypeError, ValueError):
            self.service_id = None
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

    # ------------------------------------------------------------------
    def connect(self):
        if not self.base_url or not self.service_id:
            raise XrayError("آدرس پنل Marzneshin یا شناسه سرویس (Service ID) تنظیم نشده است")
        if not (self.username and self.password):
            raise XrayError("برای اتصال به پنل Marzneshin باید یوزر/پسورد ادمین را وارد کنید")
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
        try:
            resp = self.session.request(method, url, timeout=self.timeout, verify=False, **kwargs)
        except Exception as exc:
            raise XrayError(f"اتصال به پنل Marzneshin برقرار نشد: {exc}") from exc
        if resp.status_code == 204 or not resp.content:
            return resp.status_code, {}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            raise XrayError(
                f"پاسخ پنل Marzneshin به فرمت JSON نبود (کد HTTP {resp.status_code}). "
                f"شروع پاسخ دریافتی: «{snippet or 'خالی'}»"
            )

    def _login(self):
        status, data = self._send(
            "POST",
            f"{self.base_url}/api/admins/token",
            data={"username": self.username, "password": self.password},
        )
        token = data.get("access_token")
        if status != 200 or not token:
            raise XrayError(f"ورود به پنل Marzneshin ناموفق بود: {data.get('detail') or 'یوزر/پسورد اشتباه است'}")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._logged_in = True

    def _request(self, method: str, path: str, retry: bool = True, **kwargs) -> tuple[int, dict]:
        if not self._logged_in:
            self._login()
        status, data = self._send(method, f"{self.base_url}{path}", **kwargs)
        if status == 401 and retry:
            self._logged_in = False
            self._login()
            return self._request(method, path, retry=False, **kwargs)
        return status, data

    def _get(self, path: str) -> tuple[int, dict]:
        return self._request("GET", path)

    def _post(self, path: str, body: dict) -> dict:
        status, data = self._request("POST", path, json=body)
        if status not in (200, 201):
            raise XrayError(f"درخواست به پنل Marzneshin ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")
        return data

    def _put(self, path: str, body: dict) -> dict:
        status, data = self._request("PUT", path, json=body)
        if status != 200:
            raise XrayError(f"درخواست به پنل Marzneshin ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")
        return data

    # ------------------------------------------------------------------
    def test_connection(self):
        """Logs in and confirms the configured service id actually exists
        on the panel - same purpose as MarzbanClient.test_connection's
        inbound-tag check."""
        status, data = self._get(f"/api/services/{self.service_id}")
        if status != 200:
            raise XrayError(f"سرویس با شناسه «{self.service_id}» در پنل Marzneshin پیدا نشد (کد HTTP {status})")

    def _iter_users(self, page_size: int = 200):
        page = 1
        while True:
            status, data = self._get(f"/api/users?page={page}&size={page_size}")
            if status != 200:
                return
            items = data.get("items") or []
            for u in items:
                yield u
            pages = data.get("pages")
            if len(items) < page_size or (pages is not None and page >= pages):
                return
            page += 1

    def _list_users_for_service(self) -> list[dict]:
        """All Marzneshin users assigned to self.service_id - no
        server-side "users of this service" filter on a non-sudo admin
        account (GET /api/services/{id}/users is sudo-only), so this pages
        through every user visible to this admin and filters client-side,
        same approach as MarzbanClient._list_users_for_tag."""
        return [u for u in self._iter_users() if self.service_id in (u.get("service_ids") or [])]

    @staticmethod
    def _email_of(u: dict) -> Optional[str]:
        """The original app-generated email, stashed in `note` at creation
        time (see module docstring) - falls back to the panel username for
        a user that was created directly on the panel rather than by us."""
        return u.get("note") or u.get("username")

    def list_client_emails(self, inbound_tag: str) -> list[str]:
        return [e for u in self._list_users_for_service() if (e := self._email_of(u))]

    # get_link_settings() is deliberately NOT implemented - see module
    # docstring. routers/nodes.py's sync step guards on hasattr() before
    # calling it, exactly like HiddifyClient.

    def get_online_emails(self) -> set[str]:
        """No bulk "who's online" endpoint here either (same situation as
        Marzban) - not worth listing every user just for a best-effort
        "online" badge."""
        return set()

    def list_clients_with_usage(self) -> list[dict]:
        result = []
        for u in self._list_users_for_service():
            result.append({
                "id": u.get("key"),
                "email": self._email_of(u),
                "flow": "",
                "enable": bool(u.get("enabled", True)),
                "up": 0,
                "down": int(u.get("used_traffic", 0) or 0),
                "totalGB": int(u.get("data_limit", 0) or 0),
                "expiryTime": 0,
            })
        return result

    # ------------------------------------------------------------------
    def _body_for(self, username: str, email: str, client_uuid: str) -> dict:
        return {
            "username": username,
            "key": client_uuid,
            "service_ids": [self.service_id],
            "expire_strategy": "never",
            "data_limit": 0,
            "data_limit_reset_strategy": "no_reset",
            "note": email,
        }

    def add_client(
        self,
        inbound_tag: str,  # unused - kept for interface parity with the other clients
        email: str,
        client_uuid: Optional[str] = None,
        flow: str = "",  # unused - Marzneshin has no per-request flow setting
    ) -> str:
        """Creates a Marzneshin user assigned to the configured service,
        using client_uuid as the panel's `key` (see module docstring for
        why). Returns the uuid/key used."""
        client_uuid = client_uuid or str(uuid.uuid4())
        username = sanitize_username(email)
        body = self._body_for(username, email, client_uuid)
        try:
            self._post("/api/users", body)
        except XrayError:
            # Most likely "already exists" (retried request, or a
            # sanitize_username collision with something made directly on
            # the panel) - fall through to updating it in place.
            self._put(f"/api/users/{username}", body)
        return client_uuid

    def remove_client(self, inbound_tag: str, email: str, client_uuid: Optional[str] = None):
        username = sanitize_username(email)
        status, data = self._request("DELETE", f"/api/users/{username}")
        if status not in (200, 204, 404):
            raise XrayError(f"حذف کلاینت از پنل Marzneshin ناموفق بود: {data.get('detail') or f'کد HTTP {status}'}")

    def set_client_enabled(self, inbound_tag: str, email: str, uuid_: str, flow: str, enabled: bool) -> bool:
        """POST .../enable or .../disable - self-heals by (re)creating the
        client if it's missing and the caller wants it enabled, same
        policy as MarzbanClient.set_client_enabled."""
        username = sanitize_username(email)
        status, data = self._request("POST", f"/api/users/{username}/{'enable' if enabled else 'disable'}")
        if status == 200:
            return True
        if status == 404 and enabled:
            self.add_client(inbound_tag, email, uuid_, flow)
            return True
        return False

    # ------------------------------------------------------------------
    def query_all_user_stats(self) -> dict[str, dict[str, int]]:
        results: dict[str, dict[str, int]] = {}
        for u in self._list_users_for_service():
            email = self._email_of(u)
            if not email:
                continue
            results[email] = {"uplink": 0, "downlink": int(u.get("used_traffic", 0) or 0)}
        return results
