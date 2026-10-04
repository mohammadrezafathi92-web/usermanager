"""The CENTRAL auto-approval decision and its 60-minute cap (Receipt Void
design 5.4; rollout phase P3).

Today every bot decides locally, from its own sqlite history. Here the
backend decides from the central database, so a remote bot with an empty
local history and a customer with central history get the same answer.

In this phase nothing here gates anything: the outcome is written to
auto_approval_rate_events next to the bot's own local decision, so the two
can be compared before enforcement is ever switched on (phase P9a).
The event log carries no username and no amount.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import asdict, dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from . import receipt_approval_intent as ri
from .jalali import get_display_offset

WINDOW = dt.timedelta(minutes=60)
UNKNOWN = "unknown"
SALE_KINDS = ("sale_new", "sale_renew")


@dataclass(frozen=True)
class AutoSettings:
    enabled: bool
    ignore_hours: bool
    from_hour: int
    to_hour: int
    max_amount: int
    returning_only: bool
    source: str                       # 'global' | 'owner'


def _tables():
    from .. import models_receipt_void as rv
    return rv.receipt_approvals, rv.auto_approval_subjects, rv.auto_approval_rate_events


def load_settings(db: Session, owner_admin_id: Optional[int]) -> AutoSettings:
    """The owner's own override when they configured one (enabled is not
    NULL), else the global BotSettings row - the same choice
    services/auto_approve.py makes today."""
    owner = db.get(models.AdminUser, owner_admin_id) if owner_admin_id else None
    if owner is not None and owner.own_auto_approve_enabled is not None:
        return AutoSettings(
            enabled=bool(owner.own_auto_approve_enabled), ignore_hours=bool(owner.own_auto_approve_ignore_hours),
            from_hour=owner.own_auto_approve_from_hour if owner.own_auto_approve_from_hour is not None else 9,
            to_hour=owner.own_auto_approve_to_hour if owner.own_auto_approve_to_hour is not None else 23,
            max_amount=int(owner.own_auto_approve_max_amount or 0),
            returning_only=owner.own_auto_approve_returning_only if owner.own_auto_approve_returning_only is not None else True,
            source="owner")
    row = db.get(models.BotSettings, 1)
    if row is None:
        return AutoSettings(False, False, 9, 23, 0, True, "global")
    return AutoSettings(
        enabled=bool(row.auto_approve_enabled), ignore_hours=bool(row.auto_approve_ignore_hours),
        from_hour=row.auto_approve_from_hour if row.auto_approve_from_hour is not None else 0,
        to_hour=row.auto_approve_to_hour if row.auto_approve_to_hour is not None else 0,
        max_amount=int(row.auto_approve_max_amount or 0),
        returning_only=row.auto_approve_returning_only if row.auto_approve_returning_only is not None else True,
        source="global")


def local_hour(now: dt.datetime) -> int:
    return (now + dt.timedelta(minutes=get_display_offset())).hour


def in_window(settings: AutoSettings, hour: int) -> bool:
    if settings.ignore_hours or settings.from_hour == settings.to_hour:
        return True
    if settings.from_hour < settings.to_hour:
        return settings.from_hour <= hour < settings.to_hour
    return hour >= settings.from_hour or hour < settings.to_hour


def evaluate(*, kind: str, amount: int, telegram_id: Optional[int], settings: AutoSettings, returning, hour: int
             ) -> tuple[bool, str]:
    """Pure: no I/O, no clock, no call into the bot's local decision.
    `returning` is True, False or UNKNOWN."""
    if kind not in ("new", "renew"):
        return False, "kind_not_auto_eligible"
    if not settings.enabled:
        return False, "disabled"
    if not in_window(settings, hour):
        return False, "outside_window"
    if settings.max_amount and amount > settings.max_amount:
        return False, "over_cap"
    if telegram_id is None:
        return False, "no_telegram_identity"
    if settings.returning_only:
        if returning == UNKNOWN:
            return False, "returning_unknown"
        if returning is not True:
            return False, "not_returning"
    return True, "allowed"


def history_returning(db: Session, tenant_scope_key: str, telegram_id: Optional[int],
                      exclude_approval_uuid: Optional[str] = None):
    """Has this person (tenant + Telegram) a PAID purchase on record?
    True  - a completed approval of theirs, or a non-voided card sale;
    UNKNOWN - only sales with no card evidence at all;
    False - nothing."""
    if telegram_id is None:
        return False
    approvals, _subjects, _events = _tables()
    query = select(approvals.c.id).where(
        approvals.c.tenant_scope_key == tenant_scope_key, approvals.c.telegram_id_snapshot == telegram_id,
        approvals.c.kind.in_(("new", "renew")), approvals.c.state == "completed")
    if exclude_approval_uuid:
        query = query.where(approvals.c.approval_uuid != exclude_approval_uuid)
    if db.execute(query.limit(1)).first() is not None:
        return True
    user_ids = [u.id for u in db.query(models.User).filter(models.User.telegram_id == telegram_id).all()
                if ri.tenant_scope_key(db, u.owner_admin_id) == tenant_scope_key]
    if not user_ids:
        return False
    sales = db.query(models.LedgerEntry).filter(
        models.LedgerEntry.user_id.in_(user_ids), models.LedgerEntry.kind.in_(SALE_KINDS),
        models.LedgerEntry.voided_at.is_(None))
    if sales.filter((models.LedgerEntry.payment_method == "card") | models.LedgerEntry.payment_card_id.isnot(None)).first():
        return True
    if sales.filter(models.LedgerEntry.payment_method.is_(None), models.LedgerEntry.payment_card_id.is_(None)).first():
        return UNKNOWN
    return False


def retry_after_seconds(last_granted_at: Optional[dt.datetime], now: dt.datetime) -> Optional[int]:
    """None when a grant is allowed. Exactly 60 minutes later is allowed."""
    if last_granted_at is None or last_granted_at <= now - WINDOW:
        return None
    return int(math.ceil((last_granted_at + WINDOW - now).total_seconds()))


def lock_subject(db: Session, tenant_scope_key: str, telegram_id: int) -> Optional[dict]:
    _approvals, subjects, _events = _tables()
    row = db.execute(select(subjects).where(subjects.c.tenant_scope_key == tenant_scope_key,
                                            subjects.c.telegram_id == telegram_id).with_for_update()).mappings().first()
    return dict(row) if row else None


def record_grant(db: Session, tenant_scope_key: str, telegram_id: int, approval_uuid: str, now: dt.datetime,
                 existing: Optional[dict]) -> None:
    """Same transaction as the approval INSERT. A concurrent first grant of
    the same subject raises IntegrityError, which the registration retries."""
    _approvals, subjects, _events = _tables()
    if existing is None:
        db.execute(subjects.insert().values(tenant_scope_key=tenant_scope_key, telegram_id=telegram_id,
                                            last_granted_at=now, last_approval_uuid=approval_uuid, version=0))
    else:
        db.execute(subjects.update().where(subjects.c.tenant_scope_key == tenant_scope_key,
                                           subjects.c.telegram_id == telegram_id)
                   .values(last_granted_at=now, last_approval_uuid=approval_uuid, version=subjects.c.version + 1))


def record_event(db: Session, *, tenant_scope_key: str, telegram_id: int, outcome: str, reason_code: Optional[str],
                 local_decision: Optional[str], approval_uuid: Optional[str], pending_source_instance_id: str,
                 pending_local_id: int, retry_after: Optional[int], now: dt.datetime) -> None:
    _approvals, _subjects, events = _tables()
    db.execute(events.insert().values(
        tenant_scope_key=tenant_scope_key, telegram_id=telegram_id, outcome=outcome,
        reason_code=(reason_code or None) and reason_code[:32],
        local_decision=local_decision if local_decision in ("allowed", "denied") else None,
        approval_uuid=approval_uuid, pending_source_instance_id=pending_source_instance_id,
        pending_local_id=pending_local_id, retry_after_seconds=retry_after, created_at=now))


def policy_snapshot(settings: AutoSettings, allowed: bool, reason: str, returning) -> dict:
    return {**asdict(settings), "central_allowed": allowed, "central_reason": reason,
            "returning": returning if returning in (True, False) else UNKNOWN}
