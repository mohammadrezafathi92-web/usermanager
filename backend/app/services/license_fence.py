"""Same-host restoration fence; installed before application startup work.

Every application database transaction owns a shared flock until its pooled
connection is returned (after commit/rollback). Restoration owns the exclusive
flock across fresh reads, detached node I/O and its final commit. Thus a normal
request cannot commit an administrator/quota/void decision in that interval.
The runner child only reads ownership; it does not install application hooks.
This is not distributed fencing: HA and mixed old/new writers are unsupported.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import os

from fastapi import HTTPException
from sqlalchemy import event

from .gate_locks import FileLock, lock_base_dir

_exclusive = ContextVar("license_restore_exclusive", default=False)
_installed = set()


def _lock(shared):
    try:
        return FileLock(os.path.join(lock_base_dir(), "license-runtime.lock")).acquire(
            shared=shared, timeout=10 if shared else 1)
    except (OSError, TimeoutError) as exc:
        raise HTTPException(503, "license_runtime_busy") from exc


def install(engine):
    """Call before opening application sessions, never halfway through work."""
    if engine in _installed:
        return

    @event.listens_for(engine, "begin")
    def begin(connection):
        if not _exclusive.get() and "license_runtime_lock" not in connection.info:
            connection.info["license_runtime_lock"] = _lock(True)

    @event.listens_for(engine.pool, "checkin")
    def checkin(dbapi_connection, record):
        lock = record.info.pop("license_runtime_lock", None)
        if lock is not None:
            lock.release()

    @event.listens_for(engine.pool, "invalidate")
    def invalidate(dbapi_connection, record, exception):
        # A broken socket cannot commit; release before SQLAlchemy clears
        # record.info while replacing that DBAPI connection.
        checkin(dbapi_connection, record)

    @event.listens_for(engine.pool, "close")
    def close(dbapi_connection, record):
        checkin(dbapi_connection, record)

    _installed.add(engine)


def ready(engine):
    return engine in _installed


@contextmanager
def exclusive():
    if _exclusive.get():
        yield
        return
    lock = _lock(False)
    token = _exclusive.set(True)
    try:
        yield
    finally:
        _exclusive.reset(token)
        lock.release()
