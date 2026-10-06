"""Opt-in local-host cancellation barrier; no SQLite lock across network.

Ordinary mutations hold a shared flock. Refund execution holds it exclusive
through stop/capture/delete/settlement. Restart all processes when enabling;
mixing old/uninstrumented processes is unsupported. Disabled means no locks,
queries or changed legacy behavior. This is NOT a HA/distributed fence.
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


def _acquire(exclusive):
    path = os.path.join(lock_base_dir(), "reseller-cancellation.lock")
    try:
        return FileLock(path).acquire(shared=not exclusive, timeout=10)
    except (OSError, TimeoutError) as exc:
        raise HTTPException(503, "reseller_cancellation_busy") from exc


@contextmanager
def barrier(*, exclusive=False):
    if not enabled():
        if exclusive:
            raise HTTPException(503, "reseller_cancellation_disabled")
        yield
        return
    held = _held.get()
    if held:
        if exclusive and held != "exclusive":
            raise RuntimeError("cannot upgrade a shared cancellation barrier")
        yield
        return
    lock = _acquire(exclusive)
    token = _held.set("exclusive" if exclusive else "shared")
    try:
        yield
    finally:
        _held.reset(token)
        lock.release()


def executor(fn):
    fn._refund_executor = True
    return fn


def _check_pending(fn, args, kwargs):
    if not enabled() or fn.__module__ not in ("app.routers.users", "app.routers.bot"):
        return
    from ..database import SessionLocal
    from .. import models
    from ..models_reseller_refund import ResellerRefundOperation as Operation
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
        if ids and db.query(Operation.id).filter(Operation.user_id.in_(ids), Operation.state == "pending").first():
            raise HTTPException(409, "reseller_cancellation_pending")


def guarded(fn):
    if getattr(fn, "_refund_guarded", False) or getattr(fn, "_refund_executor", False):
        return fn
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            if not enabled() or _held.get():
                return await fn(*args, **kwargs)
            import anyio
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
            with barrier():
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
