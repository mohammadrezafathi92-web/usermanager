"""Which concurrent-session rule RADIUS and the poller apply, and the
shadow record of where the per-Purchase rule would decide differently.

  legacy    (default; also with no state row) exactly the rule before the
            per-Purchase one: the User's cap over all of the customer's
            connections, else each connection's own cap
  shadow    legacy decides and acts; the per-Purchase rule is computed too
            and every disagreement is recorded - nothing else: no reject,
            kick, ban or message of its own
  enforced  the per-Purchase rule (session_cap_rule.session_scope); only
            reachable from shadow, with a fresh enforce preview and its
            explicit confirmation (session_cap_enforce)

Atomic switch. A decision holds the shared lock of one lock file (LOCK_SH)
from BEFORE it reads the mode until AFTER its final effect: for RADIUS the
reply is sent, for the poller the cycle is committed - the callers keep
the `with decision(...)` open that long. A mode change takes the lock
exclusively (LOCK_EX), which waits for every decision in flight, commits,
and only then releases and answers. So no change can fall between a
decision's reading of the rule and its effect, and once a change has been
answered every later decision uses the new mode.

No guessing. A decision that cannot get the shared lock within
READ_WAIT_SECONDS, cannot open the lock file, or cannot read a valid mode
raises ModeUnavailable and takes NO decision: RADIUS sends no reply (as for
any internal error there - the NAS retries), the poller skips the
concurrent-session step for that cycle (no kick, no release). A missing
state row is not an error: it is the documented default, legacy.
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
CHANGE_WAIT_SECONDS = 60.0
SHADOW_BUCKET_SECONDS = 300

_shadow: dict[str, dict] = {}
_shadow_lock = threading.Lock()


class ModeUnavailable(RuntimeError):
    """No valid mode under the shared lock: the caller takes no decision."""


def lock_path(base_dir: Optional[str] = None) -> str:
    return os.path.join(base_dir or lock_base_dir(), LOCK_NAME)


def _read(engine) -> str:
    try:
        with engine.connect() as conn:
            mode = conn.execute(select(models.SessionCapRuntimeState.mode).where(
                models.SessionCapRuntimeState.id == 1)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        raise ModeUnavailable(f"mode unreadable: {type(exc).__name__}") from exc
    if mode is None:
        return "legacy"  # no row: never changed, the documented default
    if mode not in MODES:
        raise ModeUnavailable(f"invalid mode {mode!r}")
    return mode


@contextmanager
def decision(engine, base_dir: Optional[str] = None, *, wait: Optional[float] = None):
    """Yields the mode under the shared lock; the caller keeps this open
    until its decision's final effect (module docstring). Raises
    ModeUnavailable instead of yielding a mode it cannot vouch for."""
    lock = FileLock(lock_path(base_dir))
    try:
        lock.acquire(shared=True, timeout=READ_WAIT_SECONDS if wait is None else wait)
    except LockTimeout as exc:
        raise ModeUnavailable("mode lock busy") from exc
    except LockUnavailable as exc:
        raise ModeUnavailable("mode lock unavailable") from exc
    try:
        mode = _read(engine)
        yield mode
    finally:
        lock.release()


def status(db) -> dict:
    row = db.get(models.SessionCapRuntimeState, 1)
    with _shadow_lock:
        pending = len(_shadow)
    lock_ok = True
    try:
        FileLock(lock_path()).acquire(shared=True, timeout=0).release()
    except LockUnavailable:
        lock_ok = False
    except LockTimeout:
        pass  # held exclusively right now: the file itself is fine
    if row is None:
        return dict(mode="legacy", epoch=0, changed_at=None, changed_by_admin_id=None, shadow_pending=pending,
                    lock_available=lock_ok)
    return dict(mode=row.mode, epoch=row.epoch, changed_at=row.changed_at, changed_by_admin_id=row.changed_by_admin_id,
                shadow_pending=pending, lock_available=lock_ok)


def change_mode(session_factory, admin_id: int, target: str, reason: str, *,
                enforce_preview: Optional[dict] = None,
                base_dir: Optional[str] = None, timeout: float = CHANGE_WAIT_SECONDS) -> dict:
    """Answered only after the change is committed AND every decision that
    could still use the old mode has finished (module docstring).

    enforced: only from shadow, and only with `enforce_preview` = {token,
    generated_at, confirmation} of a fresh session_cap_enforce.preview,
    re-verified here under the exclusive lock against the state now."""
    from . import session_cap_enforce

    reason = (reason or "").strip()
    if target not in MODES:
        raise HTTPException(422, "session_cap_mode_invalid")
    if not reason:
        raise HTTPException(422, "session_cap_mode_reason_required")
    if target == "enforced" and not enforce_preview:
        raise HTTPException(422, "enforce_preview_required")
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
            if row.mode not in MODES:
                raise HTTPException(409, "session_cap_mode_state_invalid")
            current = row.mode
            if current == target:
                db.rollback()
                with session_factory() as fresh:
                    return status(fresh)
            if target == "enforced":
                if current != "shadow":
                    raise HTTPException(409, "enforced_requires_shadow")
                session_cap_enforce.verify(db, row, **enforce_preview)
            changed_at = dt.datetime.utcnow()
            result = db.execute(models.SessionCapRuntimeState.__table__.update().where(
                models.SessionCapRuntimeState.id == 1,
                models.SessionCapRuntimeState.version == row.version).values(
                mode=target, epoch=row.epoch + 1, version=row.version + 1,
                changed_at=changed_at, changed_by_admin_id=admin_id))
            if result.rowcount != 1:
                raise HTTPException(409, "session_cap_mode_changed")
            db.add(models.SessionCapModeEvent(from_mode=current, to_mode=target, epoch=row.epoch + 1,
                                              admin_id=admin_id, reason=reason[:2000], created_at=changed_at))
            db.commit()
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
