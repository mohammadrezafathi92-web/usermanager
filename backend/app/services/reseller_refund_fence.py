"""Opt-in local-host cancellation barrier; no SQLite lock across network.

Ordinary mutations hold a shared flock. Refund execution holds it exclusive
through stop/capture/delete/settlement. Restart all processes when enabling;
mixing old/uninstrumented processes is unsupported. Disabling prevents new
cancellations. With the feature off a read-only hold lookup still runs:
previously started cancellations retain their barrier and scope checks.
This is NOT a HA/distributed fence.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import os

from fastapi import HTTPException

from ..config import settings
from .gate_locks import FileLock, lock_base_dir

_held = ContextVar("reseller_refund_fence", default=None)


def enabled():
    return settings.reseller_cancellation_enabled


def _requires_barrier():
    if enabled():
        return True
    # Turning the flag off stops NEW cancellations, not ownership of an
    # unknown/incomplete stop. Do not turn a known hold into a bypass if its
    # schema cannot be read. An installation with no operation table cannot
    # have a cancellation and retains its ordinary legacy behavior.
    from ..database import SessionLocal
    from ..models_reseller_refund import ResellerRefundOperation as Operation
    from .reseller_refund_block import holding_operations
    from sqlalchemy import inspect
    try:
        with SessionLocal() as db:
            if not inspect(db.get_bind()).has_table(Operation.__tablename__):
                return False
            return holding_operations(db).first() is not None
    except Exception:
        # A session factory or connection wrapper can fail before SQLAlchemy
        # produces an SQLAlchemyError. Unknown ownership is still not permission
        # to mutate; expose the same bounded domain error, never the raw cause.
        raise HTTPException(503, "reseller_cancellation_state_unavailable") from None


def _acquire(exclusive):
    path = os.path.join(lock_base_dir(), "reseller-cancellation.lock")
    try:
        return FileLock(path).acquire(shared=not exclusive, timeout=10)
    except (OSError, TimeoutError) as exc:
        raise HTTPException(503, "reseller_cancellation_busy") from exc


@contextmanager
def barrier(*, exclusive=False):
    if exclusive and not enabled():
        raise HTTPException(503, "reseller_cancellation_disabled")
    if not _requires_barrier():
        yield False
        return
    held = _held.get()
    if held:
        if exclusive and held != "exclusive":
            raise RuntimeError("cannot upgrade a shared cancellation barrier")
        yield True
        return
    lock = _acquire(exclusive)
    token = _held.set("exclusive" if exclusive else "shared")
    try:
        yield True
    finally:
        _held.reset(token)
        lock.release()


def executor(fn):
    fn._refund_executor = True
    return fn


def _check_pending(fn, args, kwargs):
    if fn.__module__ not in ("app.routers.users", "app.routers.bot"):
        return
    from ..database import SessionLocal
    from .. import models
    from ..models_reseller_refund import ResellerRefundOperation as Operation
    from .reseller_refund_block import holding_operations
    bound = inspect.signature(fn).bind_partial(*args, **kwargs).arguments
    ids = []
    if isinstance(bound.get("user_id"), int):
        ids.append(bound["user_id"])
    payload = bound.get("payload")
    ids.extend(getattr(payload, "user_ids", []) or [])
    username = bound.get("username") or getattr(payload, "username", None)
    with SessionLocal() as db:
        if username:
            user = db.query(models.User.id).filter(models.User.username == username).first()
            if user:
                ids.append(user.id)
        if ids and holding_operations(db).filter(Operation.user_id.in_(ids)).first():
            raise HTTPException(409, "reseller_cancellation_pending")


def guarded(fn):
    if getattr(fn, "_refund_guarded", False) or getattr(fn, "_refund_executor", False):
        return fn
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if _held.get():
                return await fn(*args, **kwargs)
            import anyio
            if not await anyio.to_thread.run_sync(_requires_barrier):
                return await fn(*args, **kwargs)
            lock = await anyio.to_thread.run_sync(lambda: _acquire(False))
            token = _held.set("shared")
            try:
                await anyio.to_thread.run_sync(lambda: _check_pending(fn, args, kwargs))
                return await fn(*args, **kwargs)
            finally:
                _held.reset(token)
                lock.release()
    else:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with barrier() as protected:
                if protected:
                    _check_pending(fn, args, kwargs)
                return fn(*args, **kwargs)
    wrapper._refund_guarded = True
    return wrapper


def wrap_routes(router, namespace=None):
    """Also replace module functions used directly by PanelBridge (no HTTP)."""
    for route in router.routes:
        if not getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}:
            continue
        original = route.endpoint
        wrapped = guarded(original)
        route.endpoint = wrapped
        route.dependant.call = wrapped
        if namespace is not None and namespace.get(original.__name__) is original:
            namespace[original.__name__] = wrapped
