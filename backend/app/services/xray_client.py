"""Manages a remote Xray-core install over SSH.

Two things are needed on the Xray server for this to work:

1. The inbound you want to hand out accounts on must have `settings.clients`
   (vless/vmess/trojan) and the inbound's `tag` must match the node's
   `xr_inbound_tag`.
2. The Xray config must have an `api` block + a `dokodemo-door` inbound on
   `xr_api_address` (default 127.0.0.1:10085) with services
   ["HandlerService", "StatsService", "LoggerService"], plus
   `stats: {}` and `policy.levels.0.statsUserUplink/Downlink = true` so
   per-user traffic is tracked. See the README for a ready-made snippet.

We deliberately avoid the Xray gRPC API for adding/removing users (that
would require compiling protobuf stubs). Instead we edit `config.json`
directly over SSH and restart the service - the same approach used by most
community panels (3x-ui, Marzban, etc). The gRPC-free `xray api statsquery`
CLI command (shipped inside the xray binary itself) is used read-only to
pull traffic counters.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Optional

import paramiko


class XrayError(Exception):
    pass


class XrayClient:
    def __init__(
        self,
        ssh_host: str,
        ssh_username: str,
        ssh_port: int = 22,
        ssh_password: Optional[str] = None,
        ssh_private_key: Optional[str] = None,
        config_path: str = "/usr/local/etc/xray/config.json",
        service_name: str = "xray",
        api_address: str = "127.0.0.1:10085",
        timeout: int = 10,
    ):
        self.ssh_host = ssh_host
        self.ssh_username = ssh_username
        self.ssh_port = ssh_port
        self.ssh_password = ssh_password
        self.ssh_private_key = ssh_private_key
        self.config_path = config_path
        self.service_name = service_name
        self.api_address = api_address
        self.timeout = timeout
        self._client: Optional[paramiko.SSHClient] = None

    # ------------------------------------------------------------------
    def connect(self):
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = None
            if self.ssh_private_key:
                from io import StringIO

                pkey = paramiko.RSAKey.from_private_key(StringIO(self.ssh_private_key))
            client.connect(
                hostname=self.ssh_host,
                port=self.ssh_port,
                username=self.ssh_username,
                password=self.ssh_password if not pkey else None,
                pkey=pkey,
                timeout=self.timeout,
            )
            self._client = client
        except Exception as exc:  # pragma: no cover - network dependent
            raise XrayError(f"اتصال SSH به سرور V2Ray برقرار نشد: {exc}") from exc
        return self

    def close(self):
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def _exec(self, command: str) -> tuple[str, str, int]:
        assert self._client is not None
        stdin, stdout, stderr = self._client.exec_command(command, timeout=self.timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out, err, exit_code

    # ------------------------------------------------------------------
    def read_config(self) -> dict:
        sftp = self._client.open_sftp()
        try:
            with sftp.open(self.config_path, "r") as f:
                data = f.read().decode("utf-8")
        finally:
            sftp.close()
        return json.loads(data)

    def write_config(self, config: dict):
        sftp = self._client.open_sftp()
        try:
            tmp_path = self.config_path + ".tmp"
            with sftp.open(tmp_path, "w") as f:
                f.write(json.dumps(config, indent=2, ensure_ascii=False))
            # atomic-ish replace + keep a backup
            out, err, code = self._exec(
                f"cp {self.config_path} {self.config_path}.bak 2>/dev/null; mv {tmp_path} {self.config_path}"
            )
            if code != 0:
                # A failure here previously went unnoticed - the caller
                # (poll/save flow) would go straight on to restart_service(),
                # potentially restarting xray with a stale or half-written
                # config and taking every client on this node offline.
                raise XrayError(f"نوشتن فایل کانفیگ روی نود ناموفق بود: {err or out}")
        finally:
            sftp.close()

    def test_connection(self):
        """Lightweight, side-effect-free check used by the "تست اتصال"
        button - just reads config.json."""
        self.read_config()

    def restart_service(self):
        out, err, code = self._exec(f"systemctl restart {self.service_name}")
        if code != 0:
            raise XrayError(f"ری‌استارت سرویس xray ناموفق بود: {err or out}")

    # ------------------------------------------------------------------
    def _find_inbound(self, config: dict, inbound_tag: str) -> dict:
        for inbound in config.get("inbounds", []):
            if inbound.get("tag") == inbound_tag:
                return inbound
        raise XrayError(f"اینباند با تگ '{inbound_tag}' در کانفیگ پیدا نشد")

    def add_client(
        self,
        inbound_tag: str,
        email: str,
        client_uuid: Optional[str] = None,
        flow: str = "",
    ) -> str:
        """Adds a client to the given inbound's `settings.clients` array and
        restarts xray. Returns the uuid used."""
        config = self.read_config()
        inbound = self._find_inbound(config, inbound_tag)
        protocol = inbound.get("protocol", "vless")
        settings = inbound.setdefault("settings", {})
        clients = settings.setdefault("clients", [])

        # remove any pre-existing entry with the same email first
        clients[:] = [c for c in clients if c.get("email") != email]

        client_uuid = client_uuid or str(uuid.uuid4())
        entry = {"id": client_uuid, "email": email}
        if protocol == "vless" and flow:
            entry["flow"] = flow
        if protocol == "trojan":
            entry = {"password": client_uuid, "email": email}

        clients.append(entry)
        self.write_config(config)
        self.restart_service()
        return client_uuid

    def remove_client(self, inbound_tag: str, email: str, client_uuid: Optional[str] = None):
        # client_uuid is unused here (config.json is filtered by email) -
        # kept for interface parity with ThreeXUIClient, which needs it.
        config = self.read_config()
        inbound = self._find_inbound(config, inbound_tag)
        clients = inbound.get("settings", {}).get("clients", [])
        clients[:] = [c for c in clients if c.get("email") != email]
        self.write_config(config)
        self.restart_service()

    def set_client_enabled(self, inbound_tag: str, email: str, uuid_: str, flow: str, enabled: bool):
        if enabled:
            self.add_client(inbound_tag, email, uuid_, flow)
        else:
            self.remove_client(inbound_tag, email)

    def get_online_emails(self) -> set[str]:
        """SSH-managed Xray installs have no equivalent to 3X-UI's
        online-clients API available here (it would need Xray's newer
        per-user online-IP stats service compiled in and enabled, which
        isn't guaranteed) - always returns "unknown" (empty set) rather
        than guessing, so callers should treat this as "can't tell", not
        "definitely offline"."""
        return set()

    # ------------------------------------------------------------------
    _STAT_LINE_RE = re.compile(r'"name":\s*"([^"]+)"[^}]*?"value":\s*"?(\d+)"?', re.S)

    def query_all_user_stats(self) -> dict[str, dict[str, int]]:
        """Returns {email: {"uplink": n, "downlink": n}} using `xray api
        statsquery`. Counters are cumulative since the xray process last
        started - callers should diff against previously stored values."""
        cmd = f"xray api statsquery -s {self.api_address} -pattern 'user>>>' -json"
        out, err, code = self._exec(cmd)
        text = out or err
        if code != 0 and not text:
            raise XrayError(f"statsquery ناموفق بود: {err}")

        results: dict[str, dict[str, int]] = {}
        for name, value in self._STAT_LINE_RE.findall(text):
            # name format: user>>>{email}>>>traffic>>>{uplink|downlink}
            parts = name.split(">>>")
            if len(parts) != 4:
                continue
            _, email, _, direction = parts
            bucket = results.setdefault(email, {"uplink": 0, "downlink": 0})
            if direction in ("uplink", "downlink"):
                bucket[direction] = int(value)
        return results


def client_for_node(node):
    """Returns an XrayClient or ThreeXUIClient for the given node, picked by
    node.xr_panel_mode. Both classes expose the same
    add_client/remove_client/set_client_enabled/query_all_user_stats
    interface plus connect()/close()/context-manager support, so call sites
    don't need to know which backend they're talking to."""
    if getattr(node, "xr_panel_mode", "ssh") == "3xui":
        from .threexui_client import ThreeXUIClient

        return ThreeXUIClient(
            node.xr_panel_base_url,
            node.xr_panel_username,
            node.xr_panel_password,
            node.xr_panel_inbound_id,
            api_token=node.xr_panel_api_token,
        )
    return XrayClient(
        node.xr_ssh_host, node.xr_ssh_username, node.xr_ssh_port,
        node.xr_ssh_password, node.xr_ssh_private_key,
        node.xr_config_path, node.xr_service_name, node.xr_api_address,
    )
