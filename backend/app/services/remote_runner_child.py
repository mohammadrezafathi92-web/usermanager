"""The killable mutation runner - CHILD side (design 10.8, 10.9).

Started only by services/remote_runner.py as
    python -m app.services.remote_runner_child <expected_parent_pid>

The DTO (with its secrets) arrives on stdin as one length-prefixed JSON
message; argv and the environment never carry any of it. The result goes
back on stdout the same way. This process imports no application model and
opens no database connection in this batch.

Exit codes (the parent treats every non-zero one, and any missing or
malformed result, as killed_unknown):
    0  a result was written
    3  the parent was already gone when the child started, or (Linux) the
       parent-death signal could not be armed - in both cases BEFORE any
       byte of the DTO is read
    4  no/short DTO on stdin
    5  DTO rejected (version, shape, unknown/unregistered action)
"""
from __future__ import annotations

import importlib
import os
import struct
import sys
import time

from . import gate_locks
from . import provisioning_child_authority as authority
from .remote_action import Outcome, RemoteActionDTO, RemoteActionError, RemoteActionResult
from .remote_runner_registry import ACTIONS

EXIT_PARENT_GONE, EXIT_NO_DTO, EXIT_BAD_DTO = 3, 4, 5
_HEADER = struct.Struct(">Q")
_MAX_MESSAGE = 16 * 1024 * 1024


PR_SET_PDEATHSIG, PR_GET_PDEATHSIG = 1, 2


def _libc_prctl():
    """prctl(2) with an explicit signature - without argtypes/restype ctypes
    would pass and return plain C ints and a failure could go unnoticed."""
    import ctypes

    prctl = ctypes.CDLL(None, use_errno=True).prctl
    prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    prctl.restype = ctypes.c_int
    return prctl


def arm_parent_death_signal(platform: str | None = None, prctl=None) -> bool:
    """Asks the kernel to SIGKILL this process when its parent goes away
    (PR_SET_PDEATHSIG), and CONFIRMS it by reading the setting back.

    Returns True only when the protection is really in place. On Linux any
    failure - the call returning non-zero, raising, or the read-back not
    showing SIGKILL - returns False, and main() then exits before reading
    the DTO: a runner that might outlive its parent must never receive a
    secret or take a lock.

    Per prctl(2), the "parent" whose death triggers the signal is the
    THREAD that created this process, not the parent process as a whole -
    which is why services/remote_runner.py forks every runner from one
    long-lived thread, so that the two mean the same thing.

    Not Linux (macOS, used for development and tests): the mechanism does
    not exist, True is returned, and only the getppid() checks in main()
    stand between an orphaned runner and its action. That is a test-only
    fallback; the design refuses 'enforced' on such a system."""
    platform = sys.platform if platform is None else platform
    if not platform.startswith("linux"):
        return True
    import ctypes
    import signal

    try:
        prctl = prctl or _libc_prctl()
        if prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0) != 0:
            return False
        current = ctypes.c_int(0)
        if prctl(PR_GET_PDEATHSIG, ctypes.addressof(current), 0, 0, 0) != 0:
            return False
        return current.value == int(signal.SIGKILL)
    except Exception:  # noqa: BLE001
        return False


def _read_message(stream) -> bytes | None:
    header = stream.read(_HEADER.size)
    if len(header) != _HEADER.size:
        return None
    (length,) = _HEADER.unpack(header)
    if length <= 0 or length > _MAX_MESSAGE:
        return None
    payload = stream.read(length)
    return payload if len(payload) == length else None


def _write_message(stream, text: str) -> None:
    payload = text.encode("utf-8")
    stream.write(_HEADER.pack(len(payload)))
    stream.write(payload)
    stream.flush()


def main(argv: list[str]) -> int:
    # Before anything heavy, before reading any secret, before any lock:
    if not arm_parent_death_signal():
        return EXIT_PARENT_GONE
    try:
        expected_parent = int(argv[1])
    except (IndexError, ValueError):
        return EXIT_PARENT_GONE
    # The parent may have died between spawning us and the prctl above -
    # in that case we were re-parented and nobody is waiting for a result.
    if os.getppid() != expected_parent:
        return EXIT_PARENT_GONE

    raw = _read_message(sys.stdin.buffer)
    if raw is None:
        return EXIT_NO_DTO
    try:
        dto = RemoteActionDTO.from_wire(raw.decode("utf-8"))
        target = ACTIONS[dto.action_type]
    except (RemoteActionError, UnicodeDecodeError, KeyError):
        return EXIT_BAD_DTO
    if dto.fencing.get("expected_parent_pid") not in (None, expected_parent):
        return EXIT_BAD_DTO

    started = time.monotonic()
    mode_lock = node_lock = None
    try:
        installation_uuid = dto.fencing.get("installation_uuid")
        if dto.node_id is not None and installation_uuid:
            # The child takes the locks ITSELF and holds them for the whole
            # action, so killing this process is what releases them.
            wait = dto.hard_deadline
            try:
                mode_lock = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid)).acquire(
                    shared=True, timeout=wait)
                node_lock = gate_locks.FileLock(gate_locks.node_lock_path(installation_uuid, dto.node_id)).acquire(
                    shared=False, timeout=wait)
            except (gate_locks.LockTimeout, gate_locks.LockUnavailable, ValueError) as exc:
                result = RemoteActionResult(
                    action_id=dto.action_id, outcome=Outcome.TRANSPORT_ERROR, write_attempted=False,
                    error_code="node_gate_timeout" if isinstance(exc, gate_locks.LockTimeout) else "node_gate_unavailable",
                )
                _write_message(sys.stdout.buffer, result.to_wire())
                return 0

        if os.getppid() != expected_parent:
            return EXIT_PARENT_GONE  # orphaned while waiting for the lock: send nothing

        module_name, function_name = target.split(":")
        try:
            with authority._runner_context(expected_parent):
                result = getattr(importlib.import_module(module_name), function_name)(dto)
            if not isinstance(result, RemoteActionResult) or result.action_id != dto.action_id:
                raise RemoteActionError("action returned something that is not its own result")
        except Exception as exc:  # noqa: BLE001
            # Only the exception's TYPE is reported: its message may quote
            # a URL, a username or a credential.
            result = RemoteActionResult(
                action_id=dto.action_id, outcome=Outcome.TRANSPORT_ERROR, write_attempted=True,
                error_code="action_raised", error_sanitized=type(exc).__name__,
            )
        elapsed = int((time.monotonic() - started) * 1000)
        result = RemoteActionResult(**{**result.__dict__, "duration_ms": elapsed})
        _write_message(sys.stdout.buffer, result.to_wire())
        return 0
    finally:
        if node_lock is not None:
            node_lock.release()
        if mode_lock is not None:
            mode_lock.release()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
