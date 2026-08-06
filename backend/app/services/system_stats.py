"""Reads THIS server's own host resource usage (CPU/RAM/disk/uptime) - used
by routers/dashboard.py to power the "وضعیت سرور" card, admin_or_above only
(see that router for why: it's the panel's own shared infrastructure, not
per-tenant data, so a level-3 Seller has no reason to see it - same
rationale as the Nodes page being Admin-tier-only).

Deliberately a single best-effort snapshot function, never raising - a
psutil hiccup (e.g. odd container /proc restrictions on some hosts) should
degrade to "این بخش نمایش داده نشد", not break the whole dashboard."""
from __future__ import annotations

import logging
import time

import psutil

logger = logging.getLogger("system_stats")

# First call to psutil.cpu_percent() with no prior baseline always returns
# 0.0 (it needs two samples to compare) - call it once at import time so
# every REAL request from here on gets a meaningful non-zero number instead
# of the dashboard's first-ever load always showing "0% CPU".
psutil.cpu_percent(interval=None)


def get_system_stats() -> dict | None:
    try:
        cpu_percent = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        uptime_seconds = int(time.time() - psutil.boot_time())
        return {
            "cpu_percent": round(cpu_percent, 1),
            "cpu_cores": psutil.cpu_count(logical=True) or 0,
            "ram_used_bytes": mem.total - mem.available,
            "ram_total_bytes": mem.total,
            "disk_used_bytes": disk.used,
            "disk_total_bytes": disk.total,
            "uptime_seconds": uptime_seconds,
        }
    except Exception:
        logger.exception("failed to read host system stats")
        return None
