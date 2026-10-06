"""node_gate - the single entry point every remote writer will pass through
(Lifecycle/P6 design v7.1, sections 10.2, 10.6, 10.7). Batch L1a-1.

STATE OF THE ROLLOUT. Every remote writer of the application now passes
through writer_gate() (batch L1a-2), but no code path can move the gate out
of 'off' (there is no endpoint, and L0's seed validation only accepts
'off') - so on a live system the only thing that happens is the one
non-blocking shared lock described below.

What the gate does in each mode:

  off       takes the SHARED gate-mode lock for the duration of the block
            and nothing else: no node lock, no GET_LOCK, no runner, no
            ownership check, no wait on other writers, no error. Two
            writers on the same node run concurrently exactly as today.
            The lock is tried ONCE, without waiting. If it cannot be taken
            (lock directory missing/unwritable, schema not ready, a mode
            change holding it exclusively at that instant) the writer
            proceeds immediately without it - 'off' must never delay or
            fail a request - and the miss is counted.
  shadow    additionally TRIES the node's exclusive lock without waiting.
            Got it -> holds it to the end. Did not -> proceeds anyway and
            counts contention. Measurement only; never a guarantee.
  enforced  not implemented in this batch: raises GateNotAvailable before
            the block runs. Fail-closed on purpose - real serialization
            needs the host-ownership check and the runner integration of
            the later batches, and pretending otherwise would be the one
            dangerous thing this module could do.

The context object placed in the ContextVar is a ShadowGateContext in 'off'
and 'shadow': proof that the path is INSTRUMENTED, with no claim of
exclusivity. (The exclusive GateToken of 'enforced' arrives with it.)
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator, Optional

from . import gate_locks

logger = logging.getLogger(__name__)

MODE_OFF, MODE_SHADOW, MODE_ENFORCED = "off", "shadow", "enforced"
# A writer never WAITS for the shared mode lock: it is tried once
# (timeout 0). Whatever waiting a safe mode transition needs is the job of
# the transition itself (it takes the lock exclusively and waits for the
# writers already inside) and belongs to the mode-transition batch - not
# to the writer path, where it would stall live requests.


class GateNotAvailable(RuntimeError):
    """The gate cannot grant what the current mode requires."""


@dataclass(frozen=True)
class GateRuntime:
    """The three singleton values the gate needs (provisioning_runtime_state)."""
    installation_uuid: str
    gate_mode: str
    gate_mode_epoch: int


@dataclass(frozen=True)
class ShadowGateContext:
    """An instrumented, NON-exclusive passage through the gate."""
    node_id: int
    mode: str                 # 'off' or 'shadow'
    gate_mode_epoch: int
    mode_lock_held: bool      # the shared gate-mode lock was really taken
    node_lock_held: bool      # 'shadow' only: the node lock happened to be free


@dataclass
class GateCounters:
    """In-process counters. Persisting them to provisioning_gate_stats is
    part of the shadow batch; here they exist so behaviour is observable
    and testable."""
    mode_lock_miss: int = 0
    contention: int = 0
    passages: int = 0
    ungated: int = 0       # writer_gate() given something that is not a saved node
    gate_error: int = 0    # the gate itself failed in 'off'; the writer ran anyway
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)

    def snapshot(self) -> dict:
        with self._lock:
            return {"mode_lock_miss": self.mode_lock_miss, "contention": self.contention,
                    "passages": self.passages, "ungated": self.ungated, "gate_error": self.gate_error}

    def reset(self) -> None:
        with self._lock:
            self.mode_lock_miss = self.contention = self.passages = self.ungated = self.gate_error = 0


counters = GateCounters()

# The singleton is re-read at most once per this many seconds per process.
# Every remote block of every writer passes through the gate, and a database
# round-trip (and, on MariaDB, a pooled connection) for each one would be a
# real cost on the poller and under load - for a value that, today, cannot
# change at all. The batch that adds gate-mode transitions has to account
# for this window (a writer may act on a mode up to this old).
RUNTIME_CACHE_SECONDS = 5.0
# The last value this process READ SUCCESSFULLY, and when it last tried. A
# failed read (database down, schema check failed) keeps the previous good
# value instead of replacing it: the gate then goes on acting on the last
# mode it really saw - so "could not read" can never quietly relax a
# stricter mode back to 'off' - and it does not hammer a struggling
# database with a retry on every passage either. Only a process that has
# never read the singleton at all treats "unreadable" as 'off'.
_cached_runtime: Optional[GateRuntime] = None
_checked_at: Optional[float] = None


def reset_cache() -> None:
    """Forgets what this process knows about the singleton (tests; and
    whatever later changes the gate mode or the installation uuid)."""
    global _cached_runtime, _checked_at
    _cached_runtime, _checked_at = None, None


_current: contextvars.ContextVar[Optional[ShadowGateContext]] = contextvars.ContextVar("node_gate_context", default=None)


def current_context() -> Optional[ShadowGateContext]:
    """The gate context of the calling code, or None outside node_gate."""
    return _current.get()


def read_runtime(session_factory: Optional[Callable] = None) -> Optional[GateRuntime]:
    """Reads the singleton. None when it cannot be trusted or read at all:
    the provisioning schema is not verified in this process, the row is
    missing, or the database errored. Never raises."""
    try:
        from .. import models_provisioning as mp
        from . import provisioning_schema

        if not provisioning_schema.is_ready():
            return None
        if session_factory is None:
            from ..database import SessionLocal as session_factory  # noqa: N813
        db = session_factory()
        try:
            row = db.get(mp.ProvisioningRuntimeState, provisioning_schema.RUNTIME_STATE_ID)
            if row is None:
                return None
            return GateRuntime(row.installation_uuid, row.gate_mode, int(row.gate_mode_epoch))
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("node_gate: could not read provisioning_runtime_state")
        return None


def _try_shared_mode_lock(installation_uuid: str) -> Optional[gate_locks.FileLock]:
    lock = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid))
    try:
        return lock.acquire(shared=True, timeout=0)
    except (gate_locks.LockUnavailable, gate_locks.LockTimeout, ValueError):
        return None


@contextlib.contextmanager
def node_gate(node_id: int, *, session_factory: Optional[Callable] = None) -> Iterator[ShadowGateContext]:
    """with node_gate(node.id): <remote writes for that node>

    See the module docstring for what each mode does. In 'off' this never
    raises and never waits on another writer."""
    if type(node_id) is not int or node_id <= 0:
        raise ValueError("node_gate needs the id of a saved node")

    # The mode comes from the (briefly cached) singleton, then the shared
    # mode lock is tried once. Making a mode CHANGE safe against writers
    # that are already running is the transition batch's job (it takes the
    # lock exclusively); nothing can change the mode yet.
    global _cached_runtime, _checked_at
    now = time.monotonic()
    if _checked_at is None or now - _checked_at >= RUNTIME_CACHE_SECONDS:
        fresh = read_runtime(session_factory)
        _checked_at = now
        if fresh is not None:
            _cached_runtime = fresh
    runtime = _cached_runtime
    mode_lock = _try_shared_mode_lock(runtime.installation_uuid) if runtime is not None else None
    node_lock: Optional[gate_locks.FileLock] = None
    try:
        mode = runtime.gate_mode if runtime is not None else MODE_OFF
        epoch = runtime.gate_mode_epoch if runtime is not None else 0

        if mode == MODE_ENFORCED:
            raise GateNotAvailable("gate_mode is 'enforced', which this build does not implement yet")
        if mode not in (MODE_OFF, MODE_SHADOW):
            raise GateNotAvailable(f"unknown gate_mode {mode!r}")

        if mode_lock is None:
            if mode == MODE_SHADOW:
                raise GateNotAvailable("shared gate-mode lock unavailable in 'shadow'")
            counters.add("mode_lock_miss")

        if mode == MODE_SHADOW:
            candidate = gate_locks.FileLock(gate_locks.node_lock_path(runtime.installation_uuid, node_id))
            try:
                node_lock = candidate.acquire(shared=False, timeout=0)
            except gate_locks.LockTimeout:
                counters.add("contention")
            except gate_locks.LockUnavailable:
                raise GateNotAvailable("node lock file unavailable in 'shadow'") from None

        context = ShadowGateContext(
            node_id=node_id, mode=mode, gate_mode_epoch=epoch,
            mode_lock_held=mode_lock is not None, node_lock_held=node_lock is not None,
        )
        counters.add("passages")
        token = _current.set(context)
        try:
            yield context
        finally:
            _current.reset(token)
    finally:
        if node_lock is not None:
            node_lock.release()
        if mode_lock is not None:
            mode_lock.release()


from .reseller_refund_fence import barrier as _refund_barrier


@contextlib.contextmanager
def _legacy_writer_gate(node) -> Iterator[Optional[ShadowGateContext]]:
    """What the existing ("legacy") remote writers use:

        with writer_gate(node), MikrotikClient.for_node(node) as mt:
            ...

    node_gate(node.id), with one extra promise for 'off': the gate can
    NEVER be the reason a legacy remote call does not happen. A node that
    is not a saved row, or any unexpected failure inside the gate itself,
    lets the block run un-gated (and is counted) instead of raising. Only
    GateNotAvailable - which 'off' never raises - is allowed out, because
    in the stricter modes "the gate could not grant this" must stop the
    write. Exceptions of the block itself always propagate unchanged."""
    node_id = getattr(node, "id", None)
    gate = None
    context = None
    if type(node_id) is int and node_id > 0:
        try:
            gate = node_gate(node_id)
            context = gate.__enter__()
        except GateNotAvailable:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("writer_gate: gate failed for node %s - continuing un-gated", node_id)
            counters.add("gate_error")
            gate = None
    else:
        counters.add("ungated")

    try:
        yield context
    except BaseException as exc:
        if gate is not None:
            try:
                gate.__exit__(type(exc), exc, exc.__traceback__)
            except BaseException as exit_exc:  # noqa: BLE001
                if exit_exc is not exc:
                    logger.exception("writer_gate: releasing the gate failed for node %s", node_id)
        raise
    else:
        if gate is not None:
            try:
                gate.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("writer_gate: releasing the gate failed for node %s", node_id)
                counters.add("gate_error")


@contextlib.contextmanager
def writer_gate(node) -> Iterator[Optional[ShadowGateContext]]:
    with _refund_barrier():
        with _legacy_writer_gate(node) as context:
            yield context
