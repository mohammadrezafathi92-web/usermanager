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
            ownership check, no wait on other writers. Two writers on the
            same node run concurrently exactly as today. A mode change
            holding the lock exclusively is waited for (never run past
            unlocked). If the lock file cannot be used at all the writer
            proceeds without it on a FRESH read of 'off', and the miss is
            counted. A mode that cannot be read - or a cached one the
            mode-state file does not prove current - refuses the passage
            (GateNotAvailable): never a stale 'off'.
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
import datetime
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


# In-process counter name -> provisioning_gate_stats column (5.12).
PERSISTED = {"contention": "contention_count", "not_instrumented": "writer_not_instrumented_count",
             "mode_lock_miss": "mode_lock_miss_count"}


@dataclass(frozen=True)
class EnforcedPassage:
    """'enforced' seen by a caller that dispatches to the runner: the parent
    holds the SHARED gate-mode lock (the mode cannot change until it is
    done) but no node lock and no write context - any inline write in this
    process is refused by transport_guard. The runner child takes the node
    lock itself."""
    node_id: int
    installation_uuid: str
    gate_mode_epoch: int


@dataclass
class GateCounters:
    """In-process counters, plus the per-node deltas not yet persisted to
    provisioning_gate_stats (flush_counters). Persisting is best-effort and
    outside every business transaction (5.12)."""
    mode_lock_miss: int = 0
    contention: int = 0
    passages: int = 0
    ungated: int = 0       # writer_gate() given something that is not a saved node
    gate_error: int = 0    # the gate itself failed in 'off'; the writer ran anyway
    not_instrumented: int = 0   # transport_guard saw a write outside node_gate in 'shadow'
    _pending: dict = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, name: str, node_id: int = 0) -> None:
        with self._lock:
            setattr(self, name, getattr(self, name) + 1)
            if name in PERSISTED:
                row = self._pending.setdefault(node_id if name != "mode_lock_miss" else 0, {})
                row[name] = row.get(name, 0) + 1

    def count_not_instrumented(self, node_id: int) -> None:
        self.add("not_instrumented", node_id)

    def take_pending(self) -> dict:
        with self._lock:
            pending, self._pending = self._pending, {}
        return pending

    def restore_pending(self, pending: dict) -> None:
        with self._lock:
            for node_id, values in pending.items():
                row = self._pending.setdefault(node_id, {})
                for name, count in values.items():
                    row[name] = row.get(name, 0) + count

    def snapshot(self) -> dict:
        with self._lock:
            return {"mode_lock_miss": self.mode_lock_miss, "contention": self.contention,
                    "passages": self.passages, "ungated": self.ungated, "gate_error": self.gate_error}

    def reset(self) -> None:
        with self._lock:
            self.mode_lock_miss = self.contention = self.passages = self.ungated = self.gate_error = 0
            self.not_instrumented = 0
            self._pending = {}


counters = GateCounters()

# Whether this build can run 'enforced': every legacy writer executed by the
# runner under the real node lock and host-ownership check (10.7, 10.8).
# The control plane refuses shadow -> enforced while this is False. Proven on
# Linux in CI (tests/test_legacy_runner_linux.py: a real runner child, its own
# gate-mode and node locks, a fake RouterOS on loopback, ownership checked by
# the child). Entering 'enforced' is still one explicit superadmin step.
ENFORCED_SUPPORTED = True

# The singleton is re-read at most once per this many seconds per process.
# Every remote block of every writer passes through the gate, and a database
# round-trip (and, on MariaDB, a pooled connection) for each one would be a
# real cost on the poller and under load - for a value that, today, cannot
# change at all. The batch that adds gate-mode transitions has to account
# for this window (a writer may act on a mode up to this old).
RUNTIME_CACHE_SECONDS = 5.0
# The last value this process READ SUCCESSFULLY, and when it last tried. A
# cached value is only ever ACTED ON when it is proven current: under the
# shared gate-mode lock, with the mode-state file present and naming its
# epoch - a missing file proves nothing. (A change writes "changing" before
# its commit and the new epoch after it, both under the exclusive lock.) Anything else - a miss of the lock, an epoch that
# moved, a change waited through - needs a fresh successful read, and a
# failed read then REFUSES the passage, in every mode including 'off': a
# cached 'off' can never let a write past a change committed meanwhile by
# another process. A failed read is retried at most once per cache window.
_cached_runtime: Optional[GateRuntime] = None
_checked_at: Optional[float] = None
_last_read_ok: bool = True


def flush_counters(session_factory: Optional[Callable] = None) -> bool:
    """Add this process's pending deltas to provisioning_gate_stats. Best
    effort, its own short transaction; a failure keeps the deltas for the
    next flush and never raises into a caller. Returns True if written."""
    pending = counters.take_pending()
    if not pending:
        return True
    written = set()                               # nodes whose deltas really committed
    try:
        from sqlalchemy import update
        from sqlalchemy.exc import IntegrityError
        from .. import models_provisioning as mp
        from . import provisioning_schema
        if not provisioning_schema.is_ready():
            raise RuntimeError("provisioning schema not ready")
        if session_factory is None:
            from ..database import SessionLocal as session_factory  # noqa: N813
        table = mp.ProvisioningGateStat.__table__
        for node_id, values in sorted(pending.items()):
            columns = {PERSISTED[name]: table.c[PERSISTED[name]] + count for name, count in values.items()}
            stamps = {"updated_at": datetime.datetime.utcnow()}
            if "contention" in values:
                stamps["last_contention_at"] = stamps["updated_at"]
            if "not_instrumented" in values:
                stamps["last_not_instrumented_at"] = stamps["updated_at"]
            for _attempt in range(2):
                db = session_factory()
                try:
                    if db.execute(update(table).where(table.c.node_id == node_id).values(
                            **columns, **stamps, version=table.c.version + 1)).rowcount == 0:
                        db.execute(table.insert().values(node_id=node_id, **{
                            PERSISTED[name]: count for name, count in values.items()}, **stamps))
                    db.commit()
                    written.add(node_id)
                    break
                except IntegrityError:            # another process inserted the row first: update it
                    db.rollback()
                finally:
                    db.close()
            if node_id not in written:
                raise RuntimeError("gate counter row could not be written")
        return True
    except Exception:  # noqa: BLE001
        # Keep exactly what did not commit - never lose a count, never count twice.
        counters.restore_pending({node: values for node, values in pending.items() if node not in written})
        logger.exception("node_gate: could not persist gate counters")
        return False


def reset_cache() -> None:
    """Forgets what this process knows about the singleton (tests; and
    whatever later changes the gate mode or the installation uuid)."""
    global _cached_runtime, _checked_at, _last_read_ok
    _cached_runtime, _checked_at, _last_read_ok = None, None, True


_current: contextvars.ContextVar[Optional[ShadowGateContext]] = contextvars.ContextVar("node_gate_context", default=None)


def current_context() -> Optional[ShadowGateContext]:
    """The gate context of the calling code, or None outside node_gate."""
    return _current.get()


def read_runtime(session_factory: Optional[Callable] = None) -> Optional[GateRuntime]:
    """The singleton, or None when there is no gate OR the read failed (use
    read_runtime_checked to tell the two apart). An unverified schema with a
    readable persisted non-off mode raises GateNotAvailable."""
    return read_runtime_checked(session_factory)[0]


def read_runtime_checked(session_factory: Optional[Callable] = None):
    """(runtime, read_ok): like read_runtime, but tells "the read worked and
    there is no gate" (None, True) apart from "the read failed" (None, False)."""
    try:
        from .. import models_provisioning as mp
        from . import provisioning_schema
        if session_factory is None:
            from ..database import SessionLocal as session_factory  # noqa: N813
        db = session_factory()
        try:
            from sqlalchemy import select
            if not provisioning_schema.is_ready():
                mode = db.execute(select(mp.ProvisioningRuntimeState.gate_mode).where(
                    mp.ProvisioningRuntimeState.id == provisioning_schema.RUNTIME_STATE_ID)).scalar_one_or_none()
                if mode is not None and mode != MODE_OFF:
                    raise GateNotAvailable("unverified schema with a persisted non-off gate")
                return None, True
            row = db.get(mp.ProvisioningRuntimeState, provisioning_schema.RUNTIME_STATE_ID)
            if row is None:
                return None, True
            return GateRuntime(row.installation_uuid, row.gate_mode, int(row.gate_mode_epoch)), True
        finally:
            db.close()
    except GateNotAvailable:
        raise
    except Exception as exc:  # noqa: BLE001
        # A schema that was never created (no table) is "no gate", not an error.
        if "no such table" in str(exc).lower() or "doesn't exist" in str(exc).lower():
            return None, True
        logger.warning("node_gate: provisioning_runtime_state unreadable (%s)", type(exc).__name__)
        return None, False


# A passage that finds the gate-mode lock held EXCLUSIVELY (a mode change is
# being committed) waits for it this long instead of running without it,
# then reads the mode again: a writer can never slip past a change unlocked.
TRANSITION_WAIT_SECONDS = 60.0


class _ModeChangeTimeout(Exception):
    pass


def _try_shared_mode_lock(installation_uuid: str) -> Optional[gate_locks.FileLock]:
    """The shared gate-mode lock, or None when the lock file cannot be used
    at all (a miss). A change in progress is WAITED for; if it does not end
    in time the passage is refused (_ModeChangeTimeout), never run unlocked."""
    lock = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid))
    try:
        return lock.acquire(shared=True, timeout=0)
    except gate_locks.LockTimeout:
        pass
    except (gate_locks.LockUnavailable, ValueError):
        return None
    lock = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid))
    try:
        held = lock.acquire(shared=True, timeout=TRANSITION_WAIT_SECONDS)
    except gate_locks.LockTimeout:
        raise _ModeChangeTimeout() from None
    except (gate_locks.LockUnavailable, ValueError):
        return None
    reset_cache()                                 # the mode may have just changed
    return held


@contextlib.contextmanager
def node_gate(node_id: int, *, session_factory: Optional[Callable] = None,
              allow_dispatch: bool = False) -> Iterator[ShadowGateContext]:
    """with node_gate(node.id): <remote writes for that node>

    See the module docstring for what each mode does. In 'off' this never
    raises and never waits on another writer."""
    if type(node_id) is not int or node_id <= 0:
        raise ValueError("node_gate needs the id of a saved node")

    # The mode comes from the (briefly cached) singleton; the shared
    # mode lock is then taken (a change in progress is waited for). The
    # cache is trusted only when proven current under that lock (see
    # _cached_runtime); otherwise the singleton is read now, and a read that
    # fails refuses the passage instead of acting on a stale mode.
    global _cached_runtime, _checked_at, _last_read_ok

    def refresh():
        global _cached_runtime, _checked_at, _last_read_ok
        fresh, ok = read_runtime_checked(session_factory)
        _checked_at, _last_read_ok = time.monotonic(), ok
        if fresh is not None:
            _cached_runtime = fresh
        return fresh, ok

    now = time.monotonic()
    runtime, verified = _cached_runtime, False
    if _checked_at is None or now - _checked_at >= RUNTIME_CACHE_SECONDS:
        fresh, ok = refresh()
        if ok:
            runtime, verified = fresh, True
    elif _cached_runtime is None and _last_read_ok:
        verified = True                           # read in this window: there is no gate
    if runtime is None and not verified:
        raise GateNotAvailable("the gate mode cannot be read; a remote write is not allowed")
    try:
        mode_lock = _try_shared_mode_lock(runtime.installation_uuid) if runtime is not None else None
    except _ModeChangeTimeout:
        raise GateNotAvailable("a gate-mode change is still in progress") from None
    node_lock: Optional[gate_locks.FileLock] = None
    try:
        if runtime is not None:
            # Under the shared lock the mode cannot change, and the state file
            # names the epoch persisted by the last change: the cache is the
            # persisted mode only when it is that epoch. Without the lock (a
            # miss), or after waiting through a change, only a fresh read
            # may decide - and if it fails nothing is sent.
            # A missing state file proves NOTHING (it may have been removed
            # after a change): only a fresh, successful read decides then.
            state = gate_locks.read_mode_state(runtime.installation_uuid) if mode_lock is not None else None
            stale = _cached_runtime is None or state is None or state != str(runtime.gate_mode_epoch)
            if (mode_lock is None and not verified) or (mode_lock is not None and stale):
                fresh, ok = refresh()
                if not ok:
                    raise GateNotAvailable("the gate mode cannot be verified; a remote write is not allowed")
                runtime = fresh
        mode = runtime.gate_mode if runtime is not None else MODE_OFF
        epoch = runtime.gate_mode_epoch if runtime is not None else 0

        if mode == MODE_ENFORCED:
            if not allow_dispatch:
                raise GateNotAvailable("gate_mode is 'enforced': this writer must run in the runner")
            if mode_lock is None:
                raise GateNotAvailable("shared gate-mode lock unavailable in 'enforced'")
            counters.add("passages")
            yield EnforcedPassage(node_id=node_id, installation_uuid=runtime.installation_uuid,
                                  gate_mode_epoch=epoch)
            return
        if mode not in (MODE_OFF, MODE_SHADOW):
            raise GateNotAvailable(f"unknown gate_mode {mode!r}")

        if mode_lock is None:
            if mode == MODE_SHADOW:
                raise GateNotAvailable("shared gate-mode lock unavailable in 'shadow'")
            counters.add("mode_lock_miss")
            # Persisted now, not on the 30 s flush: it blocks the next gate
            # change, which may run in another process. Best effort in 'off'
            # (each remote write is fenced in transport_guard regardless).
            flush_counters(session_factory)

        if mode == MODE_SHADOW:
            candidate = gate_locks.FileLock(gate_locks.node_lock_path(runtime.installation_uuid, node_id))
            try:
                node_lock = candidate.acquire(shared=False, timeout=0)
            except gate_locks.LockTimeout:
                counters.add("contention", node_id)
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
def _legacy_writer_gate(node, allow_dispatch: bool = False) -> Iterator[Optional[ShadowGateContext]]:
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
            gate = node_gate(node_id, allow_dispatch=allow_dispatch)
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


@contextlib.contextmanager
def writer_passage(node):
    """For services/legacy_remote.py only: writer_gate, except that in
    'enforced' it yields an EnforcedPassage (dispatch to the runner) instead
    of refusing. Off/shadow are exactly writer_gate."""
    with _refund_barrier():
        with _legacy_writer_gate(node, allow_dispatch=True) as context:
            yield context
