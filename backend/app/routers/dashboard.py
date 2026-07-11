import datetime as dt
from collections import OrderedDict

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_admin)])


@router.get("/stats", response_model=schemas.DashboardStats)
def stats(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    # Aggregate counts/sums in SQL instead of loading every User/Node row
    # into Python - this endpoint is polled repeatedly by the dashboard
    # page and previously did O(users) work in Python on every call.
    #
    # Non-superadmins only see stats for their own group's users - nodes/
    # online-node counts stay global (servers aren't owned by a group, they
    # can be shared across admins via package/connection assignment).
    user_q = db.query(models.User.status, func.count(models.User.id))
    usage_q = db.query(
        func.coalesce(func.sum(models.User.used_bytes), 0),
        func.coalesce(func.sum(models.User.total_quota_bytes), 0),
    )
    if not admin.is_superadmin:
        user_q = user_q.filter(models.User.owner_admin_id == admin.id)
        usage_q = usage_q.filter(models.User.owner_admin_id == admin.id)

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
    logs_q = db.query(models.UsageLog.created_at, models.UsageLog.delta_bytes).filter(
        models.UsageLog.created_at >= since
    )
    if not admin.is_superadmin:
        logs_q = logs_q.join(models.User, models.User.id == models.UsageLog.user_id).filter(
            models.User.owner_admin_id == admin.id
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
        .filter(or_(models.RadiusActiveSession.id.isnot(None), models.Connection.online.is_(True)))
    )
    if not admin.is_superadmin:
        online_users_q = online_users_q.join(models.User, models.User.id == models.Connection.user_id).filter(
            models.User.owner_admin_id == admin.id
        )
    online_users_now = online_users_q.distinct().count()

    # Rough live-speed gauge: sum of usage deltas recorded in the last 60
    # seconds, divided by 60 -> average bytes/sec. Reuses the same UsageLog
    # rows poll_all() already writes every POLL_INTERVAL_SECONDS (30s by
    # default), so this needs no new polling/sampling of its own.
    speed_since = dt.datetime.utcnow() - dt.timedelta(seconds=60)
    speed_q = db.query(func.coalesce(func.sum(models.UsageLog.delta_bytes), 0)).filter(
        models.UsageLog.created_at >= speed_since
    )
    if not admin.is_superadmin:
        speed_q = speed_q.join(models.User, models.User.id == models.UsageLog.user_id).filter(
            models.User.owner_admin_id == admin.id
        )
    bytes_last_minute = speed_q.scalar() or 0
    avg_speed_bps = bytes_last_minute / 60

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
    )
