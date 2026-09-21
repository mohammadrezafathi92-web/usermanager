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
        # A command that dies mid-flight (connection dropped, exec channel
        # timeout) raises a raw paramiko/socket exception here, not an
        # XrayError - same unreported-500 failure mode as read_config/
        # write_config above, just on the command-execution path instead of
        # the SFTP one.
        try:
            stdin, stdout, stderr = self._client.exec_command(command, timeout=self.timeout)
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        except Exception as exc:
            raise XrayError(f"اجرای دستور روی سرور از طریق SSH ناموفق بود: {exc}") from exc
        return out, err, exit_code

    # ------------------------------------------------------------------
    def _read_file(self, path: str) -> str:
        try:
            sftp = self._client.open_sftp()
        except Exception as exc:
            raise XrayError(
                f"کانال SFTP با سرور برقرار نشد (احتمالاً SFTP روی این سرور SSH غیرفعال است): {exc}"
            ) from exc
        try:
            try:
                with sftp.open(path, "r") as f:
                    return f.read().decode("utf-8")
            except FileNotFoundError:
                raise
            except Exception as exc:
                raise XrayError(f"خواندن فایل کانفیگ از روی سرور ناموفق بود: {exc}") from exc
        finally:
            sftp.close()

    def read_config(self) -> dict:
        # Every step below used to be able to raise a raw paramiko/IO/JSON
        # exception straight out of this method - connect() above wraps its
        # own failures into XrayError, but this one didn't, so a bad
        # config_path, a disabled SFTP subsystem, or a corrupt config.json
        # fell all the way through to FastAPI's default 500 handler: no
        # `detail` the panel's "تست اتصال" button or last_error field could
        # show, just a blank/generic failure with the real reason only in
        # the backend's own logs - exactly the "می‌خوره اما معلوم نیست چرا"
        # complaint this was found from (2026-09-21). Every failure mode
        # here now becomes a normal XrayError with a specific message.
        try:
            data = self._read_file(self.config_path)
        except FileNotFoundError:
            # The configured path is wrong for this server - most real
            # installs don't actually use the official install script's
            # default (/usr/local/etc/xray/config.json, this class's own
            # default too), so rather than just failing here we ask the
            # server itself where its running xray/v2ray actually reads
            # its config from and retry once with that path. On success the
            # corrected path is kept on `self.config_path` so every later
            # call in this same session (add_client/write_config/...) uses
            # it too - callers with DB access (routers/nodes.py test_node,
            # quota_manager.poll_xray_node) persist it back onto
            # Node.xr_config_path so future connections don't repeat this.
            discovered = self.discover_config_path()
            self.config_path = discovered
            data = self._read_file(discovered)
        try:
            return json.loads(data)
        except ValueError as exc:
            raise XrayError(f"فایل کانفیگ روی سرور یک JSON معتبر نیست: {exc}") from exc

    _CONFIG_ARG_RE = re.compile(r"-(?:c|config)\s+(\S+)")
    _CANDIDATE_CONFIG_PATHS = [
        "/usr/local/etc/xray/config.json",
        "/etc/xray/config.json",
        "/usr/local/etc/v2ray/config.json",
        "/etc/v2ray/config.json",
        "/opt/xray/config.json",
        "/opt/v2ray/config.json",
        "/etc/xray/conf/config.json",
        "/usr/local/x-ui/bin/config.json",
    ]

    def discover_config_path(self) -> str:
        """Best-effort autodetection of this node's real xray config.json
        path, used by read_config() when the configured xr_config_path is
        wrong. Tries, in order: (1) asking systemd what command line the
        running xray/v2ray service actually uses - the one source of truth,
        since restart_service() restarts exactly that service; (2) a list
        of paths used by common install methods; (3) a shallow filesystem
        search as a last resort. Raises XrayError with a clear explanation
        if nothing conclusive is found, rather than ever guessing silently.
        """
        tried: list[str] = []
        for svc in dict.fromkeys([self.service_name, "xray", "v2ray"]):
            out, _err, _code = self._exec(f"systemctl show -p ExecStart --no-pager {svc} 2>/dev/null; true")
            if not out.strip():
                continue
            if "-confdir" in out:
                raise XrayError(
                    f"سرویس «{svc}» روی این سرور با آرگومان «-confdir» (چند فایل کانفیگ داخل یک پوشه) اجرا "
                    "می‌شود، نه یک فایل config.json تکی - این قابلیت فعلاً فقط از یک فایل واحد پشتیبانی "
                    "می‌کند، پس مسیر را باید دستی در تنظیمات این سرور مشخص کنید."
                )
            m = self._CONFIG_ARG_RE.search(out)
            if not m:
                continue
            path = m.group(1).strip("'\"")
            if path in tried:
                continue
            tried.append(path)
            check, _err2, _code2 = self._exec(f"test -f {path} && echo __FOUND__")
            if "__FOUND__" in check:
                return path

        for path in self._CANDIDATE_CONFIG_PATHS:
            if path in tried:
                continue
            tried.append(path)
            check, _err, _code = self._exec(f"test -f {path} && echo __FOUND__")
            if "__FOUND__" in check:
                return path

        found, _err, _code = self._exec(
            "find / -xdev -maxdepth 6 -iname 'config.json' "
            "\\( -ipath '*xray*' -o -ipath '*v2ray*' \\) 2>/dev/null | head -5"
        )
        candidates = [p.strip() for p in found.splitlines() if p.strip()]
        if len(candidates) == 1:
            return candidates[0]
        if candidates:
            raise XrayError(
                "چند فایل کانفیگ احتمالی روی سرور پیدا شد و نمی‌شود مطمئن بود کدام درست است: "
                + "، ".join(candidates)
                + " - مسیر درست را دستی در تنظیمات این سرور وارد کنید."
            )
        raise XrayError(
            f"فایل کانفیگ در مسیر «{self.config_path}» روی سرور پیدا نشد و مسیر واقعی هم به‌صورت خودکار "
            "پیدا نشد (سرویس‌های systemd و مسیرهای رایج نصب بررسی شدند) - مسیر را دستی در تنظیمات این "
            "سرور وارد کنید."
        )

    def write_config(self, config: dict):
        try:
            sftp = self._client.open_sftp()
        except Exception as exc:
            raise XrayError(
                f"کانال SFTP با سرور برقرار نشد (احتمالاً SFTP روی این سرور SSH غیرفعال است): {exc}"
            ) from exc
        try:
            tmp_path = self.config_path + ".tmp"
            try:
                with sftp.open(tmp_path, "w") as f:
                    f.write(json.dumps(config, indent=2, ensure_ascii=False))
            except Exception as exc:
                raise XrayError(f"نوشتن فایل کانفیگ موقت روی سرور ناموفق بود: {exc}") from exc
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

    def list_client_emails(self, inbound_tag: str) -> list[str]:
        """Every client email currently configured on this inbound - used
        by scripts/report_orphan_connections.py to find clients with no
        matching Connection row left in the panel's DB (2026-09)."""
        config = self.read_config()
        inbound = self._find_inbound(config, inbound_tag)
        clients = inbound.get("settings", {}).get("clients", [])
        return [c.get("email") for c in clients if c.get("email")]

    def set_client_enabled(self, inbound_tag: str, email: str, uuid_: str, flow: str, enabled: bool) -> bool:
        # Unlike ThreeXUIClient's multi-endpoint fallback chain, add_client/
        # remove_client here either succeed or raise XrayError - there's no
        # silent "nothing left to try" path, so reaching this line at all
        # means the change was applied. Returns True (not None) to match
        # ThreeXUIClient's interface - see its set_client_enabled docstring
        # for why callers rely on this return value.
        if enabled:
            self.add_client(inbound_tag, email, uuid_, flow)
        else:
            self.remove_client(inbound_tag, email)
        return True

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
