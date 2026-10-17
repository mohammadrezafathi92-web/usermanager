"""Which concurrent-session rule RADIUS and the poller apply, and the
shadow record of where the per-Purchase rule would decide differently.

  legacy    (default; also with no state row) exactly the rule before the
            per-Purchase one: the User's cap over all of the customer's
            connections, else each connection's own cap
  shadow    legacy decides and acts; the per-Purchase rule is computed too
            and every disagreement is recorded - nothing else: no reject,
            kick, ban or message of its own
  enforced  the per-Purchase rule (session_cap_rule.session_scope)

Atomic switch. Every decision holds the shared lock of one lock file
(LOCK_SH) and only then reads the mode, from a fresh connection; a mode
change takes it exclusively (LOCK_EX), which waits for every decision in
flight, commits, and only then releases and answers. So once a change has
been answered, no decision runs with the old mode. A decision that cannot
get its shared hold within READ_WAIT_SECONDS (a change is holding it, not
yet answered) reads the committed mode anyway: never a refused login for a
lock. flock() serializes threads and processes alike (gate_locks.FileLock).

If the lock file cannot be opened at all, a change is refused, so the
committed mode cannot move: a decision then reads it without the lock. If
the mode cannot be read, the last mode read by this process is used
(legacy before any read).
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from contextlib import contextmanager
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import select, text

from .. import models
from .gate_locks import FileLock, LockTimeout, LockUnavailable, lock_base_dir

logger = logging.getLogger("session_cap_mode")

MODES = ("legacy", "shadow", "enforced")
LOCK_NAME = "session-cap-mode.lock"
READ_WAIT_SECONDS = 2.0
CHANGE_WAIT_SECONDS = 30.0
SHADOW_BUCKET_SECONDS = 300

_last_mode = "legacy"
_shadow: dict[str, dict] = {}
_shadow_lock = threading.Lock()


def lock_path(base_dir: Optional[str] = None) -> str:
    return os.path.join(base_dir or lock_base_dir(), LOCK_NAME)


def _read(engine) -> str:
    with engine.connect() as conn:
        mode = conn.execute(select(models.SessionCapRuntimeState.mode).where(
            models.SessionCapRuntimeState.id == 1)).scalar_one_or_none()
    return mode if mode in MODES else "legacy"


def current_mode(engine) -> str:
    global _last_mode
    try:
        mode = _read(engine)
    except Exception:  # noqa: BLE001 - never let a read failure decide a login
        logger.exception("session cap mode unreadable; using last known %r", _last_mode)
        return _last_mode
    _last_mode = mode
    return mode


@contextmanager
def decision(engine, base_dir: Optional[str] = None):
    """One enforcement decision: yields the mode it must use, holding the
    shared lock until the decision is made."""
    lock = FileLock(lock_path(base_dir))
    try:
        lock.acquire(shared=True, timeout=READ_WAIT_SECONDS)
    except (LockTimeout, LockUnavailable):
        pass  # see the module docstring: the committed mode is still right
    try:
        yield current_mode(engine)
    finally:
        lock.release()


def status(db) -> dict:
    row = db.get(models.SessionCapRuntimeState, 1)
    with _shadow_lock:
        pending = len(_shadow)
    if row is None:
        return dict(mode="legacy", epoch=0, changed_at=None, changed_by_admin_id=None, shadow_pending=pending)
    return dict(mode=row.mode if row.mode in MODES else "legacy", epoch=row.epoch, changed_at=row.changed_at,
                changed_by_admin_id=row.changed_by_admin_id, shadow_pending=pending)


def change_mode(session_factory, admin_id: int, target: str, reason: str, *,
                base_dir: Optional[str] = None, timeout: float = CHANGE_WAIT_SECONDS) -> dict:
    """Answered only after the change is committed AND every decision that
    could still use the old mode has finished (module docstring)."""
    global _last_mode
    reason = (reason or "").strip()
    if target not in MODES:
        raise HTTPException(422, "session_cap_mode_invalid")
    if not reason:
        raise HTTPException(422, "session_cap_mode_reason_required")
    lock = FileLock(lock_path(base_dir))
    try:
        lock.acquire(shared=False, timeout=timeout)
    except LockTimeout:
        raise HTTPException(409, "session_cap_mode_busy") from None
    except LockUnavailable:
        raise HTTPException(503, "session_cap_mode_lock_unavailable") from None
    try:
        with session_factory() as db:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = db.execute(select(models.SessionCapRuntimeState).where(
                models.SessionCapRuntimeState.id == 1).with_for_update()).scalar_one_or_none()
            if row is None:
                row = models.SessionCapRuntimeState(id=1, mode="legacy", epoch=0, version=0)
                db.add(row)
                db.flush()
            current = row.mode if row.mode in MODES else "legacy"
            if current == target:
                db.rollback()
                with session_factory() as fresh:
                    return status(fresh)
            changed_at = dt.datetime.utcnow()
            result = db.execute(models.SessionCapRuntimeState.__table__.update().where(
                models.SessionCapRuntimeState.id == 1,
                models.SessionCapRuntimeState.version == row.version).values(
                mode=target, epoch=row.epoch + 1, version=row.version + 1,
                changed_at=changed_at, changed_by_admin_id=admin_id))
            if result.rowcount != 1:
                db.rollback()
                raise HTTPException(409, "session_cap_mode_changed")
            db.add(models.SessionCapModeEvent(from_mode=current, to_mode=target, epoch=row.epoch + 1,
                                              admin_id=admin_id, reason=reason[:2000], created_at=changed_at))
            db.commit()
        _last_mode = target
    finally:
        lock.release()
    with session_factory() as db:
        return status(db)


# ------------------------------------------------------------------ shadow record
def note(kind: str, *, user_id: int, purchase_id: Optional[int], connection_id: Optional[int],
         legacy: tuple, new: tuple) -> None:
    """Buffers one disagreement in memory - no database write on the
    deciding path (a RADIUS reply never waits for, or fails on, it).
    flush_shadow writes the buffer after the poll cycle's own commit."""
    at = dt.datetime.utcnow()
    bucket = int(at.timestamp()) // SHADOW_BUCKET_SECONDS * SHADOW_BUCKET_SECONDS
    key = f"{kind}|{user_id}|{purchase_id or '-'}|{connection_id or '-'}|{bucket}"
    with _shadow_lock:
        entry = _shadow.get(key)
        if entry is None:
            _shadow[key] = dict(kind=kind, user_id=user_id, purchase_id=purchase_id, connection_id=connection_id,
                                bucket_start=dt.datetime.utcfromtimestamp(bucket), occurrences=1,
                                legacy_limit=legacy[0], legacy_count=legacy[1],
                                new_limit=new[0], new_count=new[1], first_at=at, last_at=at)
        else:
            entry["occurrences"] += 1
            entry["last_at"] = at
            entry["legacy_count"] = max(entry["legacy_count"] or 0, legacy[1] or 0)
            entry["new_count"] = max(entry["new_count"] or 0, new[1] or 0)


def flush_shadow(session_factory) -> int:
    """Writes the buffered disagreements in a transaction of its own; on
    failure they are logged and dropped (a shadow record, not state)."""
    with _shadow_lock:
        pending = dict(_shadow)
        _shadow.clear()
    if not pending:
        return 0
    try:
        with session_factory() as db:
            existing = {row.dedupe_key: row for row in db.query(models.SessionCapShadowEvent).filter(
                models.SessionCapShadowEvent.dedupe_key.in_(list(pending)))}
            for key, entry in pending.items():
                row = existing.get(key)
                if row is None:
                    db.add(models.SessionCapShadowEvent(dedupe_key=key, **entry))
                else:
                    row.occurrences += entry["occurrences"]
                    row.last_at = entry["last_at"]
                    row.legacy_count = max(row.legacy_count or 0, entry["legacy_count"] or 0)
                    row.new_count = max(row.new_count or 0, entry["new_count"] or 0)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("session cap shadow flush failed; %d record(s) dropped", len(pending))
        return 0
    return len(pending)
