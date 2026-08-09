"""Live resource stats (CPU/RAM/disk/uptime) for the panel's nodes -
backing the «مانیتور منابع» row on each card of the سرورها (Nodes) page.

MikroTik nodes answer over the same RouterOS API connection the panel
already uses (`/system/resource` - see MikrotikClient.get_system_resources);
SSH-managed Xray nodes answer over the same paramiko channel XrayClient
already holds, reading /proc + df directly so nothing needs installing on
the target. 3X-UI-managed Xray nodes have no shell access by design, so
they report supported=False instead of pretending.

Everything here is best-effort and short-timeout: a node being slow/down
must never hang the whole stats endpoint, so each node's failure is caught
and returned as its own {"error": ...} entry."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .. import models
from .mikrotik_client import MikrotikClient, MikrotikError
from .xray_client import XrayClient



def _mikrotik_resources(node: models.Node) -> dict:
    with MikrotikClient.for_node(node) as mt:
        return {**mt.get_system_resources(), "supported": True}


def _xray_resources(node: models.Node) -> dict:
    if getattr(node, "xr_panel_mode", "ssh") == "3xui":
        return {"supported": False, "reason": "3xui"}
    xc = XrayClient(
        node.xr_ssh_host, node.xr_ssh_username, node.xr_ssh_port or 22,
        node.xr_ssh_password, node.xr_ssh_private_key,
        node.xr_config_path or "/usr/local/etc/xray/config.json",
        node.xr_service_name or "xray", node.xr_api_address or "127.0.0.1:10085",
    )
    with xc:
        # One combined command keeps this to a single SSH round-trip.
        out, err, code = xc._exec(
            "cat /proc/loadavg; nproc; "
            "awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{print t, a}' /proc/meminfo; "
            "df -k / | tail -1 | awk '{print $2, $3}'; "
            "cut -d. -f1 /proc/uptime"
        )
        if code != 0:
            raise RuntimeError(err or out or "ssh command failed")
        lines = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
        load1 = float(lines[0].split()[0])
        cores = max(int(lines[1]), 1)
        mem_total_kb, mem_avail_kb = (int(x) for x in lines[2].split())
        disk_total_kb, disk_used_kb = (int(x) for x in lines[3].split())
        uptime_s = int(lines[4])
        days, rem = divmod(uptime_s, 86400)
        hours, rem = divmod(rem, 3600)
        return {
            "supported": True,
            # 1-minute load normalized to core count - the standard "how
            # busy is this box" figure when true per-tick CPU% would need
            # two samples over time.
            "cpu_percent": round(min(load1 / cores * 100, 100), 1),
            "mem_total": mem_total_kb * 1024,
            "mem_used": max(mem_total_kb - mem_avail_kb, 0) * 1024,
            "disk_total": disk_total_kb * 1024,
            "disk_used": disk_used_kb * 1024,
            "uptime": f"{days}d {hours}h {rem // 60}m",
        }


def fetch_node_resources(node: models.Node) -> dict:
    try:
        if node.type == models.NodeType.mikrotik:
            stats = _mikrotik_resources(node)
        else:
            stats = _xray_resources(node)
        return {"node_id": node.id, **stats}
    except (MikrotikError, Exception) as exc:  # noqa: BLE001 - per-node isolation, see module docstring
        return {"node_id": node.id, "supported": True, "error": str(exc)}


# Short-lived cache of the last result set. Every open Nodes page polls
# this endpoint, and each miss costs a fresh RouterOS API login / SSH
# handshake per node - which the device LOGS. Without this, three admins
# with the page open would triple the login noise on every router; with
# it, the devices are contacted at most once per CACHE_TTL no matter how
# many browsers are watching. (Same class of problem as the 3X-UI request
# storm fixed in threexui_client.py's _bulk_client_stats.)
CACHE_TTL = 25.0
_cache: dict[int, dict] = {}
_cache_at: float = 0.0
_cache_lock = threading.Lock()


def fetch_all(nodes: list[models.Node], force: bool = False) -> list[dict]:
    """Queries every node concurrently (they're independent machines - the
    slowest one shouldn't serialize behind the rest), reusing a result
    that's less than CACHE_TTL seconds old."""
    if not nodes:
        return []
    global _cache_at
    node_ids = [n.id for n in nodes]

    with _cache_lock:
        fresh = (time.monotonic() - _cache_at) < CACHE_TTL
        if not force and fresh and all(nid in _cache for nid in node_ids):
            return [_cache[nid] for nid in node_ids]

    with ThreadPoolExecutor(max_workers=min(len(nodes), 8)) as pool:
        results = list(pool.map(fetch_node_resources, nodes))

    with _cache_lock:
        for r in results:
            _cache[r["node_id"]] = r
        _cache_at = time.monotonic()
    return results
