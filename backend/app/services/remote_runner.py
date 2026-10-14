"""The killable mutation runner - PARENT side (design 10.8, 10.9).

run_action(dto) starts a child process, hands it the DTO over a private
pipe, waits up to the DTO's hard_deadline for a typed result, and returns
it. If the deadline passes the child is SIGKILLed and its death is
confirmed with wait() - which is the ONLY way the locks it holds are ever
released early: the kernel drops a dead process's flock. Nothing here (or
anywhere) releases a lock on behalf of a process that might still resume.

After a kill, a crash, a broken pipe or a malformed/mismatched result the
outcome is killed_unknown: the remote action may or may not have happened,
and the caller must find out by READING the remote state - it is never
reported as success and never as failure.

Batch L1a-1: nothing in the application calls run_action. Only the
self-test actions are registered, so no node can be reached through it.
"""
from __future__ import annotations

import logging
import os
import queue
import struct
import subprocess
import sys
import threading
import time

from .remote_action import ActionType, RemoteActionDTO, RemoteActionError, RemoteActionResult
from .remote_runner_registry import ACTIONS

logger = logging.getLogger(__name__)

DEFAULT_HARD_DEADLINE = 120.0
_HEADER = struct.Struct(">Q")
_CHILD_MODULE = "app.services.remote_runner_child"
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class UnregisteredAction(RemoteActionError):
    """No implementation is registered for this action_type."""


class _Spawner(threading.Thread):
    """Every runner is forked from this ONE long-lived thread.

    prctl(2) on PR_SET_PDEATHSIG: "the 'parent' in this case is considered
    to be the thread that created this process ... the signal will be sent
    when that thread terminates ... rather than after all of the threads in
    the parent process terminate". So a child forked from a short-lived
    worker thread (a request handler in the thread pool) would be killed
    when that thread ends, in the middle of its action. Forking from a
    thread that lives as long as the process makes "the parent died" mean
    the parent PROCESS died."""

    def __init__(self):
        super().__init__(name="remote-runner-spawner", daemon=True)
        self._requests: queue.Queue = queue.Queue()

    def run(self):
        while True:
            args, kwargs, reply = self._requests.get()
            try:
                reply.put((subprocess.Popen(args, **kwargs), None))
            except BaseException as exc:  # noqa: BLE001
                reply.put((None, exc))

    def popen(self, args, **kwargs) -> subprocess.Popen:
        reply: queue.Queue = queue.Queue(maxsize=1)
        self._requests.put((args, kwargs, reply))
        process, error = reply.get()
        if error is not None:
            raise error
        return process


_spawner: _Spawner | None = None
_spawner_guard = threading.Lock()

# Live runners of this process, and whether new ones are still accepted
# (clean shutdown, Lifecycle 10.4: stop starting runners, then confirm the
# end - or kill - of every live one with waitpid before releasing ownership).
_live: set = set()
_live_guard = threading.Lock()
_closing = False


class RunnerShuttingDown(RemoteActionError):
    """The process is shutting down; no new runner is started."""


def shutdown(grace_seconds: float = 5.0) -> int:
    """Refuse new runners; wait up to `grace_seconds` for live ones, then
    SIGKILL and waitpid the rest. Returns how many had to be killed."""
    global _closing
    with _live_guard:
        _closing = True
        processes = list(_live)
    deadline = time.monotonic() + grace_seconds
    killed = 0
    for process in processes:
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            killed += 1
    return killed


def live_count() -> int:
    with _live_guard:
        return sum(1 for process in _live if process.poll() is None)


def _get_spawner() -> _Spawner:
    global _spawner
    with _spawner_guard:
        if _spawner is None or not _spawner.is_alive():
            _spawner = _Spawner()
            _spawner.start()
        return _spawner


def child_command(parent_pid: int) -> list[str]:
    """The child's full argv. Deliberately a function of the parent pid and
    nothing else - no part of any DTO can end up on a command line."""
    return [sys.executable, "-m", _CHILD_MODULE, str(parent_pid)]


def run_action(dto: RemoteActionDTO) -> RemoteActionResult:
    if not isinstance(dto, RemoteActionDTO):
        raise TypeError("run_action takes a RemoteActionDTO")
    if dto.action_type not in ACTIONS:
        raise UnregisteredAction(f"no runner action registered for {dto.action_type.value!r}")
    if not isinstance(dto.action_type, ActionType):  # pragma: no cover - guaranteed by the DTO
        raise UnregisteredAction("action_type is not an ActionType")

    deadline = dto.hard_deadline or DEFAULT_HARD_DEADLINE
    payload = dto.to_wire().encode("utf-8")
    started = time.monotonic()

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    with _live_guard:
        if _closing:
            raise RunnerShuttingDown("the process is shutting down")
        process = _get_spawner().popen(
            child_command(os.getpid()),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=_BACKEND_DIR, close_fds=True,
        )
        _live.add(process)
    try:
        try:
            stdout, _ = process.communicate(_HEADER.pack(len(payload)) + payload, timeout=deadline)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()  # death confirmed - only now are its locks known to be free
            logger.error("remote runner killed after hard deadline: action=%s node=%s after %dms",
                         dto.action_type.value, dto.node_id, elapsed_ms())
            return RemoteActionResult.killed_unknown(dto.action_id, "hard_deadline_exceeded", elapsed_ms())
    except BaseException:
        # Whatever interrupted the wait, never leave a runner behind.
        process.kill()
        process.wait()
        raise
    finally:
        with _live_guard:
            _live.discard(process)

    if process.returncode != 0:
        logger.error("remote runner exited %s without a result: action=%s node=%s",
                     process.returncode, dto.action_type.value, dto.node_id)
        return RemoteActionResult.killed_unknown(dto.action_id, f"runner_exit_{process.returncode}", elapsed_ms())
    if len(stdout) < _HEADER.size:
        return RemoteActionResult.killed_unknown(dto.action_id, "no_result", elapsed_ms())
    (length,) = _HEADER.unpack(stdout[:_HEADER.size])
    body = stdout[_HEADER.size:]
    if length != len(body):
        return RemoteActionResult.killed_unknown(dto.action_id, "truncated_result", elapsed_ms())
    try:
        result = RemoteActionResult.from_wire(body.decode("utf-8"))
    except (RemoteActionError, UnicodeDecodeError):
        return RemoteActionResult.killed_unknown(dto.action_id, "invalid_result", elapsed_ms())
    if result.action_id != dto.action_id:
        return RemoteActionResult.killed_unknown(dto.action_id, "result_for_another_action", elapsed_ms())
    return result
