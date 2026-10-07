"""Private child-only write authority; not yet wired to remote transports.

The runner sets its role after arming Linux parent-death protection. A real
ChildGuard then supplies fresh host, contract, dispatch and lock proof for
EVERY writer. Context copies, forked processes and new threads cannot reuse
authority. This protects against programming mistakes, not malicious Python
that changes private registries. No real action is registered by this file.
"""
import contextlib
import contextvars
import ctypes
import os
import secrets
import signal
import sys
import threading
from dataclasses import dataclass

class WriteAuthorityUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class _Token:
    nonce: str
    pid: int
    thread_id: int


_runner = contextvars.ContextVar("provisioning_runner_role", default=None)
_current = contextvars.ContextVar("provisioning_write_authority", default=None)
_registry = {}
_lock = threading.RLock()


def _protected_parent(expected_parent):
    if not sys.platform.startswith("linux") or os.getppid() != expected_parent:
        return False
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        current = ctypes.c_int(0)
        return prctl(2, ctypes.addressof(current), 0, 0, 0) == 0 and current.value == int(signal.SIGKILL)
    except Exception:
        return False


@contextlib.contextmanager
def _runner_context(expected_parent):
    # macOS self-tests still run, but cannot acquire a writer token.
    if type(expected_parent) is not int or expected_parent < 1 or os.getppid() != expected_parent:
        raise WriteAuthorityUnavailable("runner_parent_invalid")
    mark = _runner.set((os.getpid(), threading.get_ident(), expected_parent))
    try:
        yield
    finally:
        _runner.reset(mark)


def _role():
    role = _runner.get()
    if role is None or role[:2] != (os.getpid(), threading.get_ident()) or not _protected_parent(role[2]):
        raise WriteAuthorityUnavailable("runner_write_authority_unavailable")


@contextlib.contextmanager
def _scope(guard, *, allow_writes=True):
    _role()
    if type(allow_writes) is not bool:
        raise WriteAuthorityUnavailable("runner_authority_permission_invalid")
    # Self-test runner import remains database-free. Real action authority
    # alone needs the closed read-only database/guard dependency.
    from .provisioning_child_guard import ChildGuard
    if type(guard) is not ChildGuard:
        raise WriteAuthorityUnavailable("runner_guard_invalid")
    binding = guard.check()
    current = _current.get()
    if current is not None:
        _require_guard(guard)
        with _lock:
            existing = _registry.get(current.nonce)
        if existing is None or existing[1] is not guard or existing[3] != allow_writes:
            raise WriteAuthorityUnavailable("runner_nested_guard_invalid")
        yield current
        return
    token = _Token(secrets.token_hex(32), os.getpid(), threading.get_ident())
    with _lock:
        _registry[token.nonce] = (token, guard, binding, allow_writes)
    mark = _current.set(token)
    try:
        yield token
    finally:
        with _lock:
            _registry.pop(token.nonce, None)
        _current.reset(mark)


def require_write(node_id, backend):
    """Call before any transport write; never grants off/shadow authority."""
    try:
        _role()
        token = _current.get()
        if type(node_id) is not int or node_id <= 0 or type(token) is not _Token or (
                token.pid, token.thread_id) != (os.getpid(), threading.get_ident()):
            raise WriteAuthorityUnavailable("runner_token_missing")
        with _lock:
            grant = _registry.get(token.nonce)
        if grant is None or grant[0] is not token or not grant[3] or (grant[2].node_id, grant[2].backend) != (node_id, backend):
            raise WriteAuthorityUnavailable("runner_token_binding_invalid")
        # No cached check: loss of a file/advisory lock, owner epoch, lease,
        # contract or dispatch invalidates the next writer before network I/O.
        if grant[1].check() != grant[2]:
            raise WriteAuthorityUnavailable("runner_token_binding_changed")
        return None
    except Exception:
        # Driver errors and exception messages must not expose credentials.
        raise WriteAuthorityUnavailable("runner_write_authority_unavailable") from None


def _require_guard(guard):
    """A factory cannot substitute another installation/operation's guard."""
    _role()  # Reject a fork before touching an inherited registry lock.
    token = _current.get()
    if type(token) is not _Token or (token.pid, token.thread_id) != (os.getpid(), threading.get_ident()):
        raise WriteAuthorityUnavailable("runner_write_authority_unavailable")
    with _lock:
        grant = _registry.get(token.nonce)
    if grant is None or grant[0] is not token or grant[1] is not guard:
        raise WriteAuthorityUnavailable("runner_write_authority_unavailable")
    try:
        if guard.check() != grant[2]:
            raise WriteAuthorityUnavailable("runner_guard_changed")
    except Exception:
        raise WriteAuthorityUnavailable("runner_write_authority_unavailable") from None
