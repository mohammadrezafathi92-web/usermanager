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

    PR_SET_PDEATHSIG is tied to the thread that created the child, not to
    the process: a child forked from a short-lived worker thread (a request
    handler in the thread pool) would be killed when that thread ends, in
    the middle of its action. Spawning from a thread that lives as long as
    the process makes "the parent died" mean exactly that."""

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

    process = _get_spawner().popen(
        child_command(os.getpid()),
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=_BACKEND_DIR, close_fds=True,
    )
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
