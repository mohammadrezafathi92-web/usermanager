"""Read-only history of RADIUS auth attempts rejected for exceeding the
concurrent-session limit, and of the temporary bans that follow repeated
attempts (see services/radius_server.py's HandleAuthPacket, the writer of
models.RadiusLimitEventLog). Exposed both as a dedicated panel page (all of
an admin's own users) and, filtered to one user_id, as a small section on
that user's own detail page - see this module's list_radius_logs, used by
both."""
from typing import Optional

from fastapi import APIRouter, Depends

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin
from sqlalchemy.orm import Session

router = APIRouter(prefix="/api/radius-logs", tags=["radius-logs"], dependencies=[Depends(get_current_admin)])


def _out(log: models.RadiusLimitEventLog) -> schemas.RadiusLimitEventLogOut:
    return schemas.RadiusLimitEventLogOut(
        id=log.id,
        connection_id=log.connection_id,
        user_id=log.user_id,
        username=log.username,
        connection_type=log.connection_type,
        event_type=log.event_type,
        active_count=log.active_count,
        limit_value=log.limit_value,
        banned_until=log.banned_until,
        client_ip=log.client_ip,
        created_at=log.created_at,
    )


@router.get("", response_model=list[schemas.RadiusLimitEventLogOut])
def list_radius_logs(
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
    user_id: Optional[int] = None,
    event_type: Optional[str] = None,
    limit: int = 200,
):
    """Scoped the same way the Users list is: a non-superadmin only ever
    sees events for users they own (owner_admin_id, stamped at write time -
    see radius_server.py), a superadmin sees everyone unless user_id
    narrows it further."""
    query = db.query(models.RadiusLimitEventLog)
    if not admin.is_superadmin:
        query = query.filter(models.RadiusLimitEventLog.owner_admin_id == admin.id)
    if user_id:
        query = query.filter(models.RadiusLimitEventLog.user_id == user_id)
    if event_type:
        query = query.filter(models.RadiusLimitEventLog.event_type == event_type)
    logs = query.order_by(models.RadiusLimitEventLog.id.desc()).limit(min(limit, 1000)).all()
    return [_out(l) for l in logs]
