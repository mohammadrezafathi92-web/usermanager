"""Client for the s-ui panel (github.com/alireza0/s-ui), a Go/Gin panel for
sing-box that speaks a generic action-dispatch REST API - NOT the same shape
as 3X-UI/Marzban/Hiddify/Marzneshin. Everything below was verified by reading
the actual panel source on GitHub (not docs, which are thin), specifically:
  - api/apiV2Handler.go   - route table + auth (static `Token` header, gin
    group mounted at `{base_url}apiv2/`, e.g. POST /apiv2/save, GET
    /apiv2/clients, GET /apiv2/status, GET /apiv2/onlines).
  - api/apiService.go     - Save() reads object/action/data as regular POST
    form fields (not JSON body), response envelope is always
    {"success": bool, "msg": str, "obj": ...} (api/utils.go's Msg struct).
  - service/client.go     - ClientService.Save()'s "new"/"edit"/"del" cases,
    setConfigIdentity(), preserveServerOwnedFields(), updateLinksWithFixedInbounds().
  - database/model/model.go - the Client struct (Id, Enable, Name, Config,
    Inbounds, Volume, Expiry, Up/Down, Desc/Group/Remark, ...).
  - util/genLink.go       - vlessLink() reads Config["vless"]["uuid"]/["flow"],
    confirming we (the API caller) choose the uuid ourselves - the panel never
    generates it server-side.
  - sub/sub.go, sub/subService.go - the customer subscription endpoint is a
    SEPARATE http server (its own listen address/port/domain/TLS, configured
    in the panel's own settings) serving GET {subPath}/{client.Name} - not
    reachable via the admin API's base_url, and not a secret token, just the
    client's own `name`.

Key design decisions that follow directly from the above:

* Auth is a static `Token` header checked against an in-memory list the admin
  manages from the panel's own UI (Settings > API, adds/removes tokens) - there
  is no username/password login step on our side at all, unlike every other
  panel integrated so far. The admin creates one token in s-ui and pastes it
  into this node's `xr_panel_api_token` field (the same column 3X-UI uses for
  its optional API token).

* `model.Client.Name` is the panel's one unique-per-client identifier (no
  character-set restriction beyond non-empty, unlike Marzneshin's
  ^\\w{3,32}$) and is also literally the public subscription-URL path segment
  - so we use `connection.xr_email` as-is for it, with no sanitization step
  needed (unlike marzneshin_client.sanitize_username).

* We pick our own `client_uuid` and send it as `Config = {"vless": {"uuid":
  ..., "flow": ...}}` at creation time - vlessLink() (see above) reads it back
  unchanged, so, exactly like 3X-UI/Marzban, the uuid this app already stores
  on `Connection.xr_uuid` IS the real protocol uuid, no extra indirection
  needed the way Marzneshin's `key` field required.

* `ClientService.GetAll()` (the plain `GET /apiv2/clients` list used for
  emails/usage/enable-state) deliberately excludes the `config`/`links`
  columns for size, but DOES include the panel's own auto-increment numeric
  `id` - which "edit"/"del" require (not the name). So mutating an existing
  client is a two-step "find id by name, then act on that id" - see
  `_client_by_name()`/`_client_full()` below. This mirrors the
  find-then-act fallback HiddifyClient already uses for delete-by-name.

* A single-client "edit" is a *full row replace* server-side (`tx.Save`) -
  omitted fields (Config/Inbounds/Volume/Expiry/Desc/Group/Remark) are NOT
  preserved automatically (only CreatedAt/OnlineAt/Up/Down/TotalUp/TotalDown
  are, via preserveServerOwnedFields()). So `set_client_enabled()` first does
  a by-id fetch (`GET /apiv2/clients?id=N`, which - unlike the list endpoint -
  returns every column including Config) and resends the full record with
  only `enable` flipped, rather than trying to build a partial payload.

* Like HiddifyClient and MarzneshinClient, no single "here is the vless://
  link" concept fits cleanly (the subscription server is a separate,
  independently-configured listener) - get_link_settings() is deliberately
  NOT implemented (see routers/nodes.py's hasattr() guard) and
  user_ops.get_connection_share() has a dedicated s-ui branch that hands back
  the panel's own subscription URL instead, using `node.xr_public_host` as
  the (required, since it can't be derived from the admin API's base_url) sub
  server base override - same pattern as the Hiddify/Marzneshin branches.
"""

import json
import uuid
from typing import Optional

import requests

from .xray_client import XrayError


class SuiClient:
    """Talks to a single s-ui panel install. `inbound_id` is
    `node.xr_panel_inbound_id` reused as the target sing-box inbound's
    numeric id (s-ui clients can be attached to several inbounds at once via
    `Inbounds`, but this app manages one node = one inbound like every other
    panel mode)."""

    def __init__(self, base_url, api_token, inbound_id, timeout=10):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_token = (api_token or "").strip()
        try:
            self.inbound_id = int(inbound_id) if inbound_id not in (None, "") else None
        except (TypeError, ValueError):
            self.inbound_id = None
        self.timeout = timeout
        self.session: Optional[requests.Session] = None

    def connect(self):
        if not self.base_url or not self.api_token or not self.inbound_id:
            raise XrayError(
                "برای اتصال به پنل s-ui، آدرس پنل، توکن API و شناسه اینباند لازم است"
            )
        self.session = requests.Session()
        self.session.headers["Token"] = self.api_token
        self.test_connection()
        return self

    def close(self):
        if self.session is not None:
            self.session.close()
            self.session = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # ------------------------------------------------------------- wire

    def _url(self, path: str) -> str:
        return f"{self.base_url}/apiv2/{path}"

    def _parse(self, resp: requests.Response):
        if resp.status_code != 200:
            raise XrayError(f"پنل s-ui خطای HTTP {resp.status_code} برگرداند")
        try:
            body = resp.json()
        except ValueError as exc:
            raise XrayError("پاسخ نامعتبر از پنل s-ui") from exc
        if not body.get("success"):
            raise XrayError(body.get("msg") or "خطای نامشخص از پنل s-ui")
        return body.get("obj")

    def _get(self, action: str, params: Optional[dict] = None):
        try:
            resp = self.session.get(self._url(action), params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise XrayError(f"ارتباط با پنل s-ui برقرار نشد: {exc}") from exc
        return self._parse(resp)

    def _post_save(self, obj: str, act: str, data):
        body = {"object": obj, "action": act, "data": json.dumps(data, ensure_ascii=False)}
        try:
            resp = self.session.post(self._url("save"), data=body, timeout=self.timeout)
        except requests.RequestException as exc:
            raise XrayError(f"ارتباط با پنل s-ui برقرار نشد: {exc}") from exc
        return self._parse(resp)

    def test_connection(self):
        self._get("status")

    # ------------------------------------------------------------- reads

    def _all_clients(self) -> list:
        obj = self._get("clients") or {}
        return obj.get("clients") or []

    @staticmethod
    def _inbounds_of(raw: dict) -> list:
        v = raw.get("inbounds")
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                v = []
        return v if isinstance(v, list) else []

    def _client_by_name(self, name: str) -> Optional[dict]:
        for c in self._all_clients():
            if c.get("name") == name:
                return c
        return None

    def _client_full(self, client_id) -> Optional[dict]:
        """Unlike _all_clients()'s list endpoint, GET .../clients?id=N returns
        every column (including config/links) - needed before an edit."""
        obj = self._get("clients", params={"id": client_id}) or {}
        clients = obj.get("clients") or []
        return clients[0] if clients else None

    def list_client_emails(self, inbound_tag) -> list:
        return [
            c.get("name")
            for c in self._all_clients()
            if self.inbound_id in self._inbounds_of(c) and c.get("name")
        ]

    # get_link_settings() is deliberately NOT implemented - see module
    # docstring. routers/nodes.py's sync step guards on hasattr() before
    # calling it, exactly like HiddifyClient and MarzneshinClient.

    def get_online_emails(self) -> set:
        try:
            obj = self._get("onlines") or {}
        except XrayError:
            return set()
        users = obj.get("User") or obj.get("user") or []
        return set(u for u in users if u)

    def list_clients_with_usage(self) -> list:
        out = []
        for c in self._all_clients():
            if self.inbound_id not in self._inbounds_of(c):
                continue
            out.append({
                "id": None,
                "email": c.get("name"),
                "flow": "",
                "enable": bool(c.get("enable")),
                "up": c.get("up") or 0,
                "down": c.get("down") or 0,
                "totalGB": c.get("volume") or 0,
                "expiryTime": c.get("expiry") or 0,
            })
        return out

    def query_all_user_stats(self) -> dict:
        stats = {}
        for c in self._all_clients():
            email = c.get("name")
            if not email:
                continue
            stats[email] = {"uplink": c.get("up") or 0, "downlink": c.get("down") or 0}
        return stats

    # ------------------------------------------------------------- writes

    def add_client(self, inbound_tag, email, client_uuid=None, flow=""):
        client_uuid = client_uuid or str(uuid.uuid4())
        existing = self._client_by_name(email)
        payload = {
            "name": email,
            "enable": True,
            "config": {"vless": {"uuid": client_uuid, "flow": flow or ""}},
            "inbounds": [self.inbound_id],
            "volume": 0,
            "expiry": 0,
            "desc": "",
            "group": "",
            "remark": "",
        }
        if existing:
            payload["id"] = existing["id"]
            self._post_save("clients", "edit", payload)
        else:
            self._post_save("clients", "new", payload)
        return client_uuid

    def remove_client(self, inbound_tag, email, client_uuid=None):
        existing = self._client_by_name(email)
        if not existing:
            return
        self._post_save("clients", "del", existing["id"])

    def set_client_enabled(self, inbound_tag, email, uuid_, flow, enabled) -> bool:
        existing = self._client_by_name(email)
        if not existing:
            if enabled:
                self.add_client(inbound_tag, email, uuid_, flow)
                return True
            return False
        full = self._client_full(existing["id"]) or existing
        full["enable"] = bool(enabled)
        self._post_save("clients", "edit", full)
        return True
