"""File-lock primitives of the node ownership gate (Lifecycle/P6 design
v7.1, sections 10.2 and 10.6). Pure: no database, no network, no import of
any application model - the runner child uses this module too.

Two kinds of lock file live under {lock_dir}/{installation_uuid}/:

- gate-mode.lock      shared (LOCK_SH) by every remote writer for the whole
                      of its remote work, exclusive (LOCK_EX) only while the
                      gate mode is being changed
- node-{id}.lock      exclusive per node - the actual serialization, taken
                      only in 'shadow' (non-blocking) and 'enforced'

flock() locks belong to the open file description, so every hold opens its
OWN descriptor: two threads of one process and two separate processes are
serialized alike, and the kernel releases the lock when the holder dies.
None of these locks has a TTL, and nothing here can release a lock held by
somebody else.
"""
from __future__ import annotations

import errno
import fcntl
import hashlib
import os
import time
import uuid
from typing import Optional

GATE_MODE_LOCK_NAME = "gate-mode.lock"
_POLL_SECONDS = 0.02


class LockUnavailable(OSError):
    """The lock file could not be opened/created at all (missing or
    unwritable directory). Distinct from a timeout."""


class LockTimeout(TimeoutError):
    """The lock is held by someone else and the wait ran out."""


def lock_base_dir() -> str:
    """UM_LOCK_DIR, or `locks/` next to the persisted secret key - the data
    directory the application already has to be able to write to."""
    configured = os.environ.get("UM_LOCK_DIR", "").strip()
    if configured:
        return configured
    secret_key_file = os.environ.get("SECRET_KEY_FILE", "/app/data/.secret_key")
    return os.path.join(os.path.dirname(secret_key_file), "locks")


def _canonical_uuid(value: str) -> str:
    # The uuid becomes a directory name - it must be exactly a uuid, so it
    # can never be '..' or contain a path separator.
    if not isinstance(value, str) or str(uuid.UUID(value)) != value:
        raise ValueError("installation_uuid is not a canonical UUID")
    return value


def installation_dir(installation_uuid: str, base_dir: Optional[str] = None) -> str:
    return os.path.join(base_dir or lock_base_dir(), _canonical_uuid(installation_uuid))


def mode_lock_path(installation_uuid: str, base_dir: Optional[str] = None) -> str:
    return os.path.join(installation_dir(installation_uuid, base_dir), GATE_MODE_LOCK_NAME)


def node_lock_path(installation_uuid: str, node_id: int, base_dir: Optional[str] = None) -> str:
    if type(node_id) is not int or node_id <= 0:
        raise ValueError("node_id must be a positive integer")
    return os.path.join(installation_dir(installation_uuid, base_dir), f"node-{node_id}.lock")


def get_lock_name(installation_uuid: str, node_id: int) -> str:
    """Name for MariaDB's GET_LOCK (design 10.2). GET_LOCK names are global
    to the whole server, so the installation is part of the name; always 60
    characters (the limit is 64)."""
    if type(node_id) is not int or node_id <= 0:
        raise ValueError("node_id must be a positive integer")
    digest = hashlib.sha256(f"um-gate|{_canonical_uuid(installation_uuid)}|{node_id}".encode("ascii")).hexdigest()
    return "um1:" + digest[:56]


class FileLock:
    """One hold of one lock file. Not reusable and not re-entrant: a new
    hold is a new FileLock (a new descriptor)."""

    def __init__(self, path: str):
        self.path = path
        self._fd: Optional[int] = None
        self.shared: Optional[bool] = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, *, shared: bool, timeout: Optional[float]) -> "FileLock":
        """timeout=None waits forever, 0 tries exactly once. Raises
        LockUnavailable if the file cannot be opened, LockTimeout if the
        lock is still held elsewhere when the wait ends."""
        if self._fd is not None:
            raise RuntimeError("this FileLock is already held")
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:
            raise LockUnavailable(exc.errno, f"cannot open lock file: {exc.strerror}") from None
        operation = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, operation)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                        raise LockUnavailable(exc.errno, f"flock failed: {exc.strerror}") from None
                if deadline is not None and time.monotonic() >= deadline:
                    raise LockTimeout(f"lock still held after {timeout}s")
                time.sleep(_POLL_SECONDS)
        except BaseException:
            os.close(fd)
            raise
        self._fd, self.shared = fd, shared
        return self

    def release(self) -> None:
        """Closing the descriptor is what releases the lock - there is no
        other way, and no way to release someone else's."""
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __enter__(self) -> "FileLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
