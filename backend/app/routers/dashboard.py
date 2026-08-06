import datetime as dt
from collections import OrderedDict

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin
from ..services import hierarchy, system_stats

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_admin)])


@router.get("/stats", response_model=schemas.DashboardStats)
def stats(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    # Aggregate counts/sums in SQL instead of loading every User/Node row
    # into Python - this endpoint is polled repeatedly by the dashboard
    # page and previously did O(users) work in Python on every call.
    #
    # Every user-related figure below is scoped through hierarchy.
    # owned_admin_ids - which, per the 3-tier hierarchy's isolation rule
    # (see that function's docstring), is NOT unrestricted for a
    # superadmin either: a superadmin's dashboard only reflects users they
    # personally created themselves, never any Admin's or Seller's own
    # customer base. An Admin (level 2) sees their own tree's combined
    # stats (themself + their Sellers). Node/online-node counts stay
    # global regardless of tier - servers aren't customer data, they're
    # shared infrastructure a superadmin configures and grants out.
    # Superadmin's stats ALSO include never-assigned/orphaned users
    # (owner_admin_id IS NULL) - see hierarchy.user_visibility_clause.
    visibility = hierarchy.user_visibility_clause(db, admin)
    user_q = db.query(models.User.status, func.count(models.User.id)).filter(visibility)
    usage_q = db.query(
        func.coalesce(func.sum(models.User.used_bytes), 0),
        func.coalesce(func.sum(models.User.total_quota_bytes), 0),
    ).filter(visibility)

    user_counts = user_q.group_by(models.User.status).all()
    counts_by_status = {status: count for status, count in user_counts}
    total_users = sum(counts_by_status.values())

    total_used_bytes, total_quota_bytes = usage_q.first()

    total_nodes = db.query(func.count(models.Node.id)).scalar() or 0
    online_nodes = (
        db.query(func.count(models.Node.id))
        .filter(models.Node.last_error.is_(None), models.Node.last_seen.isnot(None))
        .scalar()
        or 0
    )

    since = dt.datetime.utcnow() - dt.timedelta(hours=24)
    logs_q = (
        db.query(models.UsageLog.created_at, models.UsageLog.delta_bytes)
        .join(models.User, models.User.id == models.UsageLog.user_id)
        .filter(models.UsageLog.created_at >= since, visibility)
    )
    logs = logs_q.all()

    # Bucket in Python so this works identically on sqlite/postgres/mysql.
    buckets: "OrderedDict[str, int]" = OrderedDict()
    now = dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for i in range(23, -1, -1):
        bucket_time = now - dt.timedelta(hours=i)
        buckets[bucket_time.strftime("%Y-%m-%d %H:00")] = 0
    for created_at, delta_bytes in logs:
        key = created_at.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%d %H:00")
        if key in buckets:
            buckets[key] += int(delta_bytes or 0)

    # Distinct users currently connected via either path: an open RADIUS
    # session (openvpn/l2tp, pushed live by the RADIUS server on
    # Start/Stop) or a xray connection the node last reported online
    # (Connection.online, refreshed once per poll_xray_node cycle).
    online_users_q = (
        db.query(models.Connection.user_id)
        .outerjoin(models.RadiusActiveSession, models.RadiusActiveSession.connection_id == models.Connection.id)
        .join(models.User, models.User.id == models.Connection.user_id)
        .filter(
            or_(models.RadiusActiveSession.id.isnot(None), models.Connection.online.is_(True)),
            visibility,
        )
    )
    online_users_now = online_users_q.distinct().count()

    # Rough live-speed gauge: sum of usage deltas recorded in the last 60
    # seconds, divided by 60 -> average bytes/sec. Reuses the same UsageLog
    # rows poll_all() already writes every POLL_INTERVAL_SECONDS (30s by
    # default), so this needs no new polling/sampling of its own.
    speed_since = dt.datetime.utcnow() - dt.timedelta(seconds=60)
    speed_q = (
        db.query(func.coalesce(func.sum(models.UsageLog.delta_bytes), 0))
        .join(models.User, models.User.id == models.UsageLog.user_id)
        .filter(models.UsageLog.created_at >= speed_since, visibility)
    )
    bytes_last_minute = speed_q.scalar() or 0
    avg_speed_bps = bytes_last_minute / 60

    # Per-protocol connection counts (same visibility scope as everything
    # else) - powers the dashboard's VPN-services-style status grid. Fixed
    # dict with every ConnectionType key present (defaulting to 0) so the
    # frontend can render a stable grid without guessing which protocols
    # exist yet.
    protocol_counts = {p.value: 0 for p in models.ConnectionType}
    protocol_rows = (
        db.query(models.Connection.type, func.count(models.Connection.id))
        .join(models.User, models.User.id == models.Connection.user_id)
        .filter(visibility)
        .group_by(models.Connection.type)
        .all()
    )
    for conn_type, count in protocol_rows:
        key = conn_type.value if hasattr(conn_type, "value") else conn_type
        protocol_counts[key] = count

    # Host system stats (CPU/RAM/disk of THIS server) - shared
    # infrastructure, not tenant data, so only shown to a superadmin or
    # level-2 Admin (see schemas.DashboardStats' docstring on these fields).
    sys_stats = system_stats.get_system_stats() if hierarchy.role(admin) != hierarchy.ROLE_SELLER else None

    return schemas.DashboardStats(
        total_users=total_users,
        active_users=counts_by_status.get(models.UserStatus.active, 0),
        disabled_users=counts_by_status.get(models.UserStatus.disabled, 0),
        quota_exceeded_users=counts_by_status.get(models.UserStatus.quota_exceeded, 0),
        total_nodes=total_nodes,
        online_nodes=online_nodes,
        online_users_now=online_users_now,
        total_used_bytes=total_used_bytes,
        total_quota_bytes=total_quota_bytes,
        usage_last_24h=[{"bucket": k, "bytes": v} for k, v in buckets.items()],
        admin_balance=None if admin.is_superadmin else (admin.balance or 0),
        avg_speed_bps=avg_speed_bps,
        protocol_connection_counts=protocol_counts,
        system_cpu_percent=sys_stats["cpu_percent"] if sys_stats else None,
        system_cpu_cores=sys_stats["cpu_cores"] if sys_stats else None,
        system_ram_used_bytes=sys_stats["ram_used_bytes"] if sys_stats else None,
        system_ram_total_bytes=sys_stats["ram_total_bytes"] if sys_stats else None,
        system_disk_used_bytes=sys_stats["disk_used_bytes"] if sys_stats else None,
        system_disk_total_bytes=sys_stats["disk_total_bytes"] if sys_stats else None,
        system_uptime_seconds=sys_stats["uptime_seconds"] if sys_stats else None,
    )
