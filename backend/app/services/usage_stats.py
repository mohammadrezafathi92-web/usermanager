"""Shared time-bucketing for usage charts - used by both the admin-wide
dashboard chart (routers/dashboard.py) and the per-user breakdown on
UserDetail (routers/users.py), so the two draw from identical bucket
boundaries/labels and neither re-derives the same math.
"""
from __future__ import annotations

import datetime as dt
from collections import OrderedDict

from sqlalchemy.orm import Session

from .. import models

# How far back each tab looks and how coarse to bucket. Hourly for 24h
# stays readable at 24 points; 7d/30d would be 168/720 points at hourly
# resolution, so those bucket by day instead (7 and 30 points respectively).
USAGE_RANGES = {
    "24h": {"hours": 24, "step": dt.timedelta(hours=1), "fmt": "%Y-%m-%d %H:00"},
    "7d": {"hours": 24 * 7, "step": dt.timedelta(days=1), "fmt": "%Y-%m-%d"},
    "30d": {"hours": 24 * 30, "step": dt.timedelta(days=1), "fmt": "%Y-%m-%d"},
}


def usage_buckets(db: Session, extra_filter, range_key: str) -> "OrderedDict[str, dict]":
    """Sums UsageLog.delta_bytes/upload_bytes/download_bytes into
    fixed-width time buckets covering the requested range, oldest first,
    every bucket present (zero-filled) even with no traffic in it - chart
    axes depend on a stable, gap-free x-axis.

    `extra_filter` scopes which UsageLog rows count - an admin-tree
    visibility clause for the dashboard's own chart, or a plain
    `UsageLog.user_id == X` for one user's own page. Callers are
    responsible for having already checked the caller can see whatever
    `extra_filter` is scoped to.
    """
    spec = USAGE_RANGES[range_key]
    step = spec["step"]
    num_buckets = spec["hours"] // int(step.total_seconds() // 3600)
    now = dt.datetime.utcnow()
    # Floor "now" to the bucket width - current (partial) hour/day is its
    # own bucket, so live traffic always lands in the newest one.
    anchor = now.replace(minute=0, second=0, microsecond=0) if step == dt.timedelta(hours=1) \
        else now.replace(hour=0, minute=0, second=0, microsecond=0)

    buckets: "OrderedDict[str, dict]" = OrderedDict()
    for i in range(num_buckets - 1, -1, -1):
        buckets[(anchor - i * step).strftime(spec["fmt"])] = {"bytes": 0, "upload_bytes": 0, "download_bytes": 0}
    since = anchor - (num_buckets - 1) * step

    logs = (
        db.query(
            models.UsageLog.created_at, models.UsageLog.delta_bytes,
            models.UsageLog.upload_bytes, models.UsageLog.download_bytes,
        )
        .join(models.User, models.User.id == models.UsageLog.user_id)
        .filter(models.UsageLog.created_at >= since, extra_filter)
        .all()
    )
    for created_at, delta_bytes, upload_bytes, download_bytes in logs:
        key_dt = created_at.replace(minute=0, second=0, microsecond=0) if step == dt.timedelta(hours=1) \
            else created_at.replace(hour=0, minute=0, second=0, microsecond=0)
        key = key_dt.strftime(spec["fmt"])
        if key in buckets:
            b = buckets[key]
            b["bytes"] += int(delta_bytes or 0)
            b["upload_bytes"] += int(upload_bytes or 0)
            b["download_bytes"] += int(download_bytes or 0)
    return buckets
