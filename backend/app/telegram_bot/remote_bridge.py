"""HTTP counterpart to panel_bridge.py's PanelBridge - implements the exact
same async interface (same method names, same argument/return shapes) but
by calling the mother server's own `/api/bot/*` HTTP API with an X-API-Key
instead of touching the database directly. Selected automatically by
panel_bridge.py when the PANEL_API_URL environment variable is set - which
is how a bot deployed on a SECOND, remote server (see
services/remote_deploy.py) still operates on the real, single source of
truth data instead of some empty local database.

Every handler in telegram_bot/handlers/*.py only ever does
`from ..panel_bridge import api, ApiError` and calls `api.xxx(...)` - since
this class mirrors PanelBridge's interface exactly, none of that handler
code needs to know or care whether it's running in-process or remotely."""
from __future__ import annotations

import asyncio
from typing import Optional

import requests


class RemoteBridge:
    def __init__(self, base_url: str, api_key: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> dict:
        return {"X-API-Key": self.api_key}

    def _request(self, method: str, path: str, **kwargs):
        from .panel_bridge import ApiError  # local import - see module docstring for why

        url = f"{self.base_url}{path}"
        try:
            resp = requests.request(method, url, headers=self._headers(), timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise ApiError(f"اتصال به پنل اصلی برقرار نشد: {exc}") from exc

        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise ApiError(str(detail))

        if not resp.content:
            return None
        return resp.json()

    async def _call(self, method: str, path: str, **kwargs):
        return await asyncio.to_thread(self._request, method, path, **kwargs)

    # ---------------------------------------------------------------- nodes
    async def list_nodes(self) -> list[dict]:
        return await self._call("GET", "/nodes")

    # ------------------------------------------------------------ packages
    async def list_packages(self) -> list[dict]:
        return await self._call("GET", "/packages")

    async def get_package_files(self, package_id: int) -> list[dict]:
        """Filename + raw bytes for each file attached to the package -
        fetches the file list from list_packages()'s already-loaded
        `files` field, then downloads each file's bytes over HTTP (this
        server has no local disk access to the mother server's uploads)."""
        packages = await self.list_packages()
        pkg = next((p for p in packages if p["id"] == package_id), None)
        if not pkg:
            return []
        out = []
        for f in pkg.get("files", []):
            content = await asyncio.to_thread(self._download, f"/packages/{package_id}/files/{f['id']}/download")
            if content is not None:
                out.append({"filename": f["filename"], "content": content})
        return out

    def _download(self, path: str) -> Optional[bytes]:
        url = f"{self.base_url}{path}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=self.timeout)
        except requests.RequestException:
            return None
        if resp.status_code >= 400:
            return None
        return resp.content

    async def get_payment_info(self) -> dict:
        return await self._call("GET", "/payment-info")

    # -------------------------------------------------------- tutorials
    async def list_tutorials(self) -> list[dict]:
        return await self._call("GET", "/tutorials")

    async def get_tutorial_media(self, tutorial_id: int) -> list[dict]:
        tutorials = await self.list_tutorials()
        tutorial = next((t for t in tutorials if t["id"] == tutorial_id), None)
        if not tutorial:
            return []
        out = []
        for m in tutorial.get("media", []):
            content = await asyncio.to_thread(
                self._download, f"/tutorials/{tutorial_id}/media/{m['id']}/download"
            )
            if content is not None:
                out.append({"kind": m["kind"], "filename": m["filename"], "content": content})
        return out

    # ---------------------------------------------------------- broadcast
    async def list_telegram_user_ids(self) -> list[int]:
        return await self._call("GET", "/telegram-user-ids")

    # ---------------------------------------------------------------- users
    async def create_user(
        self,
        username: str,
        full_name: Optional[str] = None,
        quota_gb: float = 0,
        expire_days: Optional[int] = None,
        telegram_id: Optional[int] = None,
        connections: Optional[list[dict]] = None,
        owner_admin_id: Optional[int] = None,
        package_name: Optional[str] = None,
        package_id: Optional[int] = None,
    ) -> dict:
        payload = {
            "username": username,
            "full_name": full_name,
            "quota_gb": quota_gb,
            "expire_days": expire_days,
            "telegram_id": telegram_id,
            "connections": connections or [],
            "owner_admin_id": owner_admin_id,
            "package_name": package_name,
            "package_id": package_id,
        }
        return await self._call("POST", "/users", json=payload)

    async def get_user(self, username: str, owner_admin_id: Optional[int] = None) -> dict:
        params = {"owner_admin_id": owner_admin_id} if owner_admin_id is not None else {}
        return await self._call("GET", f"/users/{username}", params=params)

    async def get_user_by_telegram(self, telegram_id: int) -> Optional[dict]:
        from .panel_bridge import ApiError

        try:
            return await self._call("GET", f"/users/by-telegram/{telegram_id}")
        except ApiError:
            return None

    async def list_users_by_telegram(self, telegram_id: int) -> list[dict]:
        from .panel_bridge import ApiError

        try:
            return await self._call("GET", f"/users/by-telegram/{telegram_id}/all")
        except ApiError:
            return []

    async def get_admin_by_telegram(self, telegram_id: int) -> Optional[dict]:
        from .panel_bridge import ApiError

        try:
            return await self._call("GET", f"/admin-by-telegram/{telegram_id}")
        except ApiError:
            return None

    async def list_users(
        self, page: int = 1, page_size: int = 8, search: Optional[str] = None, owner_admin_id: Optional[int] = None
    ) -> dict:
        params = {"page": page, "page_size": page_size}
        if search:
            params["search"] = search
        if owner_admin_id is not None:
            params["owner_admin_id"] = owner_admin_id
        return await self._call("GET", "/users", params=params)

    async def link_telegram(self, username: str, telegram_id: int) -> dict:
        return await self._call("POST", f"/users/{username}/link-telegram", json={"telegram_id": telegram_id})

    async def add_connection(
        self, username: str, node_id: int, protocol: str, flow: str = "", owner_admin_id: Optional[int] = None,
        purchase_batch: Optional[str] = None, package_name: Optional[str] = None,
    ) -> dict:
        payload = {
            "node_id": node_id, "protocol": protocol, "flow": flow,
            "purchase_batch": purchase_batch, "package_name": package_name,
        }
        params = {"owner_admin_id": owner_admin_id} if owner_admin_id is not None else {}
        return await self._call("POST", f"/users/{username}/connections", json=payload, params=params)

    async def renew(
        self, username: str, add_gb: float = 0, add_days: int = 0, reset_usage: bool = False,
        owner_admin_id: Optional[int] = None, package_id: Optional[int] = None,
    ) -> dict:
        payload = {"add_gb": add_gb, "add_days": add_days, "reset_usage": reset_usage, "package_id": package_id}
        params = {"owner_admin_id": owner_admin_id} if owner_admin_id is not None else {}
        return await self._call("POST", f"/users/{username}/renew", json=payload, params=params)

    async def reset_usage(self, username: str, owner_admin_id: Optional[int] = None) -> dict:
        params = {"owner_admin_id": owner_admin_id} if owner_admin_id is not None else {}
        return await self._call("POST", f"/users/{username}/reset-usage", params=params)

    async def set_enabled(self, username: str, enabled: bool, owner_admin_id: Optional[int] = None) -> dict:
        params = {"enabled": enabled}
        if owner_admin_id is not None:
            params["owner_admin_id"] = owner_admin_id
        return await self._call("POST", f"/users/{username}/set-enabled", params=params)

    async def add_balance(self, username: str, amount: int) -> dict:
        return await self._call("POST", f"/users/{username}/add-balance", json={"amount": amount})

    async def delete_user(self, username: str, owner_admin_id: Optional[int] = None) -> None:
        params = {"owner_admin_id": owner_admin_id} if owner_admin_id is not None else {}
        await self._call("DELETE", f"/users/{username}", params=params)
