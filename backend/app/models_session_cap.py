"""Staged rollout of the per-Purchase concurrent-session cap (owner,
2026-10-17; session_cap_rule.session_scope).

  session_cap_runtime_state  the one mode row: legacy (default, also when
                             the row is missing) | shadow | enforced
  session_cap_mode_events    audit of every mode change
  session_cap_shadow_events  what the per-Purchase rule WOULD have decided
                             differently while legacy was enforced, one row
                             per (kind, scope, connection, 5-minute bucket)
  purchase_cap_repairs       audit of every repaired Purchase cap

Brand-new tables on the project's own Base: create_all makes them, nothing
existing is altered.
"""
from sqlalchemy import Column, DateTime, Integer, String, Text

from .models import Base, now


class SessionCapRuntimeState(Base):
    __tablename__ = "session_cap_runtime_state"

    id = Column(Integer, primary_key=True)  # always 1
    mode = Column(String(10), nullable=False, default="legacy")
    epoch = Column(Integer, nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    changed_at = Column(DateTime, nullable=True)
    changed_by_admin_id = Column(Integer, nullable=True)


class SessionCapModeEvent(Base):
    __tablename__ = "session_cap_mode_events"

    id = Column(Integer, primary_key=True)
    from_mode = Column(String(10), nullable=False)
    to_mode = Column(String(10), nullable=False)
    epoch = Column(Integer, nullable=False)
    admin_id = Column(Integer, nullable=False)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)


class SessionCapShadowEvent(Base):
    __tablename__ = "session_cap_shadow_events"

    id = Column(Integer, primary_key=True)
    # kind|user|purchase|connection|bucket - one row per 5 minutes and key
    dedupe_key = Column(String(160), nullable=False, unique=True)
    kind = Column(String(32), nullable=False, index=True)
    user_id = Column(Integer, nullable=False, index=True)
    purchase_id = Column(Integer, nullable=True, index=True)
    connection_id = Column(Integer, nullable=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    occurrences = Column(Integer, nullable=False, default=1)
    legacy_limit = Column(Integer, nullable=True)
    legacy_count = Column(Integer, nullable=True)
    new_limit = Column(Integer, nullable=True)
    new_count = Column(Integer, nullable=True)
    first_at = Column(DateTime, nullable=False)
    last_at = Column(DateTime, nullable=False)


class PurchaseCapRepair(Base):
    __tablename__ = "purchase_cap_repairs"

    id = Column(Integer, primary_key=True)
    purchase_id = Column(Integer, nullable=False, index=True)
    user_id = Column(Integer, nullable=False)
    # max_concurrent_sessions | reserved_max_concurrent_sessions (NULL on
    # rows from before the second existed: the first)
    field = Column(String(40), nullable=True)
    before_value = Column(Integer, nullable=True)
    after_value = Column(Integer, nullable=False)
    user_cap_at_repair = Column(Integer, nullable=True)
    scope_connection_ids = Column(Text, nullable=False)  # JSON list, as previewed
    preview_token = Column(String(64), nullable=False)
    reason = Column(Text, nullable=False)
    admin_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=now, nullable=False)
