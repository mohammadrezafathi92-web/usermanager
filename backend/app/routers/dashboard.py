import datetime as dt
from collections import OrderedDict

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin
from ..services import accounting, hierarchy, jalali, system_stats

# How far ahead the dashboard looks for services about to lapse. A week is
# long enough to act on and short enough that the number stays meaningful.
EXPIRING_SOON_DAYS = 7

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

    # ---- "what needs me today" ------------------------------------------
    # The dashboard used to be a wall of totals with nothing actionable on
    # it. These four groups answer the questions actually asked when the
    # panel is opened in the morning: what is waiting on me, what can I
    # sell, how did we do, and is anything broken.
    now = dt.datetime.utcnow()
    soon = now + dt.timedelta(days=EXPIRING_SOON_DAYS)

    # Services about to lapse - each one is a renewal conversation. Counted
    # per Purchase (the per-service model) plus legacy users still on the
    # shared pool, so nobody is missed during the transition.
    expiring_purchases = (
        db.query(func.count(models.Purchase.id))
        .join(models.User, models.User.id == models.Purchase.user_id)
        .filter(visibility)
        .filter(models.Purchase.status == models.UserStatus.active)
        .filter(models.Purchase.expire_at.isnot(None))
        .filter(models.Purchase.expire_at > now, models.Purchase.expire_at <= soon)
        .scalar() or 0
    )
    expiring_legacy = (
        db.query(func.count(models.User.id))
        .filter(visibility)
        .filter(models.User.status == models.UserStatus.active)
        .filter(models.User.expire_at.isnot(None))
        .filter(models.User.expire_at > now, models.User.expire_at <= soon)
        .scalar() or 0
    )

    # Money. Scoped exactly like the accounting section, so a seller sees
    # their own numbers and never the panel's.
    ledger_q = accounting.scoped_query(db, admin).filter(
        models.LedgerEntry.kind.in_(("sale_new", "sale_renew"))
    )
    # "Today" and "this month" must be bounded by the LOCAL day, not the UTC
    # one. Rows are stored in UTC, so the local midnight is computed and then
    # converted back to UTC for the comparison. Without this, at 00:52 UTC -
    # which is 04:22 in Tehran - everything sold since the local midnight
    # (i.e. after 20:30 UTC yesterday) counted as yesterday, and "sales today"
    # read 0 next to a healthy monthly total.
    offset = dt.timedelta(minutes=jalali.get_display_offset())
    local_now = now + offset
    today_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0) - offset
    local_month_start = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_start = local_month_start - offset
    prev_month_end = month_start
    prev_month_start = (local_month_start - dt.timedelta(days=1)).replace(day=1) - offset

    def _sum(q):
        return int(q.with_entities(func.coalesce(func.sum(models.LedgerEntry.amount), 0)).scalar() or 0)

    sales_today = _sum(ledger_q.filter(models.LedgerEntry.created_at >= today_start))
    sales_month = _sum(ledger_q.filter(models.LedgerEntry.created_at >= month_start))
    sales_prev_month = _sum(
        ledger_q.filter(
            models.LedgerEntry.created_at >= prev_month_start,
            models.LedgerEntry.created_at < prev_month_end,
        )
    )

    # Infrastructure: which nodes are actually down, by name - a bare
    # "2/3 online" tells you something is wrong but not what.
    offline_node_names = [
        name for (name,) in (
            db.query(models.Node.name)
            .filter(models.Node.enabled == True)  # noqa: E712
            # Same definition of "online" as the count above: seen at least
            # once, and not currently reporting an error.
            .filter(or_(models.Node.last_error.isnot(None), models.Node.last_seen.is_(None)))
            .all()
        )
    ]

    return schemas.DashboardStats(
        expiring_soon_users=expiring_purchases + expiring_legacy,
        expiring_soon_days=EXPIRING_SOON_DAYS,
        sales_today=sales_today,
        sales_month=sales_month,
        sales_prev_month=sales_prev_month,
        offline_node_names=offline_node_names,
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
