"""Manages users on a SoftEther VPN Server's Virtual Hub over its own
JSON-RPC 2.0 admin API (github.com/SoftEtherVPN/SoftEtherVPN) - no SSH, no
third-party panel.

The panel only manages Password-auth users inside ONE existing Virtual Hub.
Creating that hub, and turning on whichever tunneling modes the admin wants
(SSTP/L2TP/OpenVPN/SSL-VPN) is done directly on the SoftEther server, same
"panel manages the credential, admin configures the actual VPN service"
philosophy used for MikroTik's OpenVPN/L2TP/IKEv2/SSTP fields.

API notes (confirmed against SoftEther's own generated TypeScript client
stub, developer_tools/vpnserver-jsonrpc-clients/.../vpnrpc.ts, since the
README only lists method names, not field names):

  Endpoint: HTTPS POST to https://{host}:{port}/api/
  Auth headers (sent on every call): X-VPNADMIN-HUBNAME, X-VPNADMIN-PASSWORD
    - HUBNAME = the target hub's name, PASSWORD = that hub's admin password
      (or the server-wide admin password, if the server has no per-hub
      password set - either way it's `se_admin_password` here).
  Envelope: request {jsonrpc: "2.0", id, method, params}, response either
    {jsonrpc, id, result} or {jsonrpc, id, error: {code, message}}.

  Methods used here, with their exact request/response field names:
    GetHubStatus  {HubName_str} -> hub info incl. NumUsers_u32 - used as a
                  side-effect-free connectivity + hub-existence check.
    CreateUser    {HubName_str, Name_str, GroupName_str, Realname_utf,
                  Note_utf, AuthType_u32 (Password=1), Auth_Password_str,
                  UsePolicy_bool, "policy:Access_bool"}
    SetUser       same shape as CreateUser - SoftEther's own docs say this
                  is also how you flip a user's access on/off ("set the
                  user's security policy to deny access instead of
                  deleting a user"), via UsePolicy_bool + policy:Access_bool.
                  Auth_Password_str is write-only (never returned by
                  GetUser) and SetUser replaces the whole record, so every
                  call here re-sends AuthType_u32/Auth_Password_str too -
                  otherwise a plain enable/disable toggle would silently
                  reset the user to Anonymous auth and lock them out.
    DeleteUser    {HubName_str, Name_str}
    EnumUser      {HubName_str} -> {UserList: [{Name_str, AuthType_u32,
                  DenyAccess_bool, IsTrafficFilled_bool,
                  "Ex.Recv.BroadcastBytes_u64", "Ex.Recv.UnicastBytes_u64",
                  "Ex.Send.BroadcastBytes_u64", "Ex.Send.UnicastBytes_u64",
                  ...}]} - bulk per-hub traffic counters, one call for every
                  user instead of one GetUser per user.
    EnumSession   {HubName_str} -> {SessionList: [{Username_str, ...}]} -
                  live sessions, used to derive which usernames are online.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

AUTH_TYPE_PASSWORD = 1


class SoftEtherError(Exception):
    pass


class SoftEtherClient:
    def __init__(
        self,
        host: str,
        port: int,
        hub_name: str,
        admin_password: str,
        verify_tls: bool = False,
        timeout: int = 10,
    ):
        self.host = (host or "").strip()
        self.port = port or 443
        self.hub_name = (hub_name or "").strip()
        self.admin_password = admin_password or ""
        self.verify_tls = verify_tls
        self.timeout = timeout
        self.base_url = f"https://{self.host}:{self.port}/api/"
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        self._id = 0

    # ------------------------------------------------------------------
    def connect(self):
        if not self.host or not self.hub_name:
            raise SoftEtherError("آدرس سرور یا نام هاب SoftEther تنظیم نشده است")
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
    def _call(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        body = {
            "jsonrpc": "2.0",
            "id": str(self._id),
            "method": method,
            "params": params or {},
        }
        headers = {
            "X-VPNADMIN-HUBNAME": self.hub_name,
            "X-VPNADMIN-PASSWORD": self.admin_password,
        }
        try:
            resp = self.session.post(
                self.base_url, headers=headers, data=json.dumps(body),
                timeout=self.timeout, verify=self.verify_tls,
            )
        except Exception as exc:
            raise SoftEtherError(f"اتصال به سرور SoftEther برقرار نشد: {exc}") from exc
        try:
            data = resp.json()
        except ValueError:
            snippet = (resp.text or "").strip().replace("\n", " ")[:200]
            raise SoftEtherError(
                f"پاسخ سرور SoftEther به فرمت JSON نبود (کد HTTP {resp.status_code}). "
                f"احتمالا آدرس/پورت مدیریت یا رمز هاب اشتباه است. "
                f"شروع پاسخ دریافتی: «{snippet or 'خالی'}»"
            )
        error = data.get("error")
        if error:
            raise SoftEtherError(f"خطای SoftEther ({error.get('code')}): {error.get('message')}")
        return data.get("result") or {}

    # ------------------------------------------------------------------
    def test_connection(self):
        """Lightweight, side-effect-free check used by the "تست اتصال"
        button - confirms the admin credentials and hub name are both
        correct by asking for the hub's own status."""
        self._call("GetHubStatus", {"HubName_str": self.hub_name})

    # ------------------------------------------------------------------
    def add_client(self, username: str, password: str):
        self._call("CreateUser", {
            "HubName_str": self.hub_name,
            "Name_str": username,
            "GroupName_str": "",
            "Realname_utf": "",
            "Note_utf": "",
            "AuthType_u32": AUTH_TYPE_PASSWORD,
            "Auth_Password_str": password,
            "UsePolicy_bool": True,
            "policy:Access_bool": True,
        })

    def remove_client(self, username: str):
        try:
            self._call("DeleteUser", {"HubName_str": self.hub_name, "Name_str": username})
        except SoftEtherError as exc:
            # Tolerate "already gone" the same way the other node clients
            # do (e.g. ThreeXUIClient.remove_client) - the DB row is being
            # deleted regardless, and a user manually removed on the
            # SoftEther server directly shouldn't block that.
            if "not exist" in str(exc).lower() or "does not exist" in str(exc).lower():
                return
            raise

    def set_client_enabled(self, username: str, password: str, enabled: bool) -> bool:
        """Toggles access via SetUser's policy:Access_bool WITHOUT ever
        deleting the user - SoftEther's own recommended way to temporarily
        deny a user ("set the user's security policy to deny access
        instead of deleting a user"). `password` must be the connection's
        current plaintext password (stored in Connection.ppp_password) -
        SetUser replaces the whole user record and never returns the
        existing password back to re-send it, so it has to come from the
        caller. Returns whether the change is confirmed applied, same
        contract as ThreeXUIClient.set_client_enabled - callers only record
        the new state in the DB when this returns True."""
        try:
            self._call("SetUser", {
                "HubName_str": self.hub_name,
                "Name_str": username,
                "GroupName_str": "",
                "Realname_utf": "",
                "Note_utf": "",
                "AuthType_u32": AUTH_TYPE_PASSWORD,
                "Auth_Password_str": password,
                "UsePolicy_bool": True,
                "policy:Access_bool": enabled,
            })
            return True
        except SoftEtherError as exc:
            # Self-heal like the other clients: if the user doesn't exist
            # at all (e.g. an earlier add_client failed partway), only
            # recreate it when ENABLING - never delete-to-disable.
            if enabled and ("not exist" in str(exc).lower() or "does not exist" in str(exc).lower()):
                self.add_client(username, password)
                return True
            logger.warning("softether set_client_enabled failed for %r: %s", username, exc)
            return False

    # ------------------------------------------------------------------
    def query_all_user_stats(self) -> dict[str, dict[str, int]]:
        """Returns {username: {"uplink": n, "downlink": n}} - cumulative
        totals since the hub last reset that user's counter. One bulk
        EnumUser call for the whole hub, mirroring xray's
        query_all_user_stats (which exists specifically to avoid a
        per-user request storm - see ThreeXUIClient._bulk_client_stats's
        docstring for the incident that pattern was born from).

        uplink = Recv (hub received FROM the user = the user's upload),
        downlink = Send (hub sent TO the user = the user's download) - the
        same "server's own point of view" convention RouterOS/xray use.
        Users whose IsTrafficFilled_bool is false this cycle are left out
        of the result entirely rather than reported as a zero delta -
        callers (poll_softether_node) skip anything missing, so this is
        safely idempotent across cycles that happen to miss a user."""
        result = self._call("EnumUser", {"HubName_str": self.hub_name})
        stats: dict[str, dict[str, int]] = {}
        for item in result.get("UserList") or []:
            name = item.get("Name_str")
            if not name or not item.get("IsTrafficFilled_bool"):
                continue
            uplink = int(item.get("Ex.Recv.BroadcastBytes_u64", 0) or 0) + int(item.get("Ex.Recv.UnicastBytes_u64", 0) or 0)
            downlink = int(item.get("Ex.Send.BroadcastBytes_u64", 0) or 0) + int(item.get("Ex.Send.UnicastBytes_u64", 0) or 0)
            stats[name] = {"uplink": uplink, "downlink": downlink}
        return stats

    def get_online_emails(self) -> set[str]:
        """Returns the set of usernames with a currently open VPN session
        on this hub - named get_online_emails (not get_online_usernames)
        purely for interface parity with XrayClient/ThreeXUIClient, which
        poll_softether_node relies on to stay a drop-in alongside
        poll_xray_node. Best-effort: on any failure returns an empty set
        rather than raising, same as ThreeXUIClient.get_online_emails."""
        try:
            result = self._call("EnumSession", {"HubName_str": self.hub_name})
        except SoftEtherError:
            return set()
        return {
            item.get("Username_str")
            for item in result.get("SessionList") or []
            if item.get("Username_str")
        }


def client_for_node(node) -> SoftEtherClient:
    return SoftEtherClient(
        node.se_host, node.se_port, node.se_hub_name, node.se_admin_password,
        verify_tls=bool(getattr(node, "se_verify_tls", False)),
    )
