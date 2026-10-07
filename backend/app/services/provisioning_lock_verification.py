"""Explicit local/advisory lock self-tests; never mutate a node or a mode.

No startup hook or live caller. The caller supplies its externally read host
identity and holds the gate-mode lock exclusively through verification, the
ownership transaction and its commit. Linux local-filesystem policy is a deny
list, not a proof that every filesystem is safe. No heartbeat takeover.
"""
import datetime as dt
import hashlib
import os
import re
from pathlib import Path
import select
import subprocess
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import text

from . import gate_locks
from .provisioning_host import HostIdentity


class VerificationFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class VerifiedLocks:
    identity: HostIdentity
    installation_uuid: str
    backend: str
    checked_at: dt.datetime

    def __post_init__(self):
        if not isinstance(self.identity, HostIdentity) or self.backend not in ("flock", "flock+get_lock") or (
                not isinstance(self.checked_at, dt.datetime) or
                str(uuid.UUID(self.installation_uuid)) != self.installation_uuid):
            raise VerificationFailed("lock_verification_invalid")


def require_exclusive(hold, installation_uuid, base_dir):
    expected = gate_locks.mode_lock_path(installation_uuid, base_dir)
    if not isinstance(hold, gate_locks.FileLock) or not hold.held or hold.shared is not False or hold.path != expected:
        raise VerificationFailed("gate_mode_exclusive_required")


def _local_filesystem(directory):
    if sys.platform != "linux":
        raise VerificationFailed("lock_verification_requires_linux")
    target = str(Path(directory).resolve(strict=True))
    mounts = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            left, right = line.split(" - ", 1)
            mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), left.split()[4])
            if "\\" in re.sub(r"\\([0-7]{3})", "", left.split()[4]):
                raise VerificationFailed("lock_filesystem_unverified")
            if target == mount or target.startswith(mount.rstrip("/") + "/"):
                mounts.append((len(mount), right.split()[0]))
    except (OSError, ValueError, IndexError):
        raise VerificationFailed("lock_filesystem_unverified") from None
    # Stacked mounts can have the same path. A lexical filesystem-name
    # tie-break is not proof of which mount backs the lock descriptors.
    longest = max((length for length, _ in mounts), default=-1)
    candidates = [kind for length, kind in mounts if length == longest]
    if len(candidates) != 1 or candidates[0] in ("nfs", "nfs4", "cifs", "smb", "smb3", "9p") or (
            candidates[0].startswith("fuse")):
        raise VerificationFailed("lock_filesystem_unverified")


def _flock(path):
    first, second = gate_locks.FileLock(path), gate_locks.FileLock(path)
    try:
        first.acquire(shared=False, timeout=0)
        try:
            second.acquire(shared=False, timeout=0)
        except gate_locks.LockTimeout:
            pass
        else:
            raise VerificationFailed("lock_exclusion_failed")
        first.release()
        second.acquire(shared=False, timeout=0)
    finally:
        first.release()
        second.release()
    # A killed process must release its own descriptor. No unlink/manual
    # unlock can make this test pass while a child is still alive.
    script = """import fcntl,os,sys,time
fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600)
fcntl.flock(fd,fcntl.LOCK_EX)
print('locked',flush=True)
time.sleep(30)
"""
    child = subprocess.Popen([sys.executable, "-c", script, path], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, close_fds=True)
    try:
        if not select.select([child.stdout], [], [], 5)[0] or child.stdout.readline() != b"locked\n":
            raise VerificationFailed("lock_child_unverified")
        try:
            second.acquire(shared=False, timeout=0)
        except gate_locks.LockTimeout:
            pass
        else:
            raise VerificationFailed("lock_child_exclusion_failed")
        child.kill()
        child.wait(timeout=5)
        if child.returncode != -9:
            raise VerificationFailed("lock_child_death_unverified")
        second.acquire(shared=False, timeout=0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stdout.close()
        second.release()


def _get_lock(engine, name):
    first = second = None
    try:
        first, second = engine.connect(), engine.connect()
        statement = text("SELECT GET_LOCK(:name,0)")
        if first.execute(statement, dict(name=name)).scalar_one() != 1 or (
                second.execute(statement, dict(name=name)).scalar_one() != 0):
            raise VerificationFailed("advisory_lock_exclusion_failed")
        # SQLAlchemy close() may return a live DBAPI connection to its pool.
        # Invalidate really disconnects, which must release this advisory lock.
        first.invalidate()
        first.close()
        if second.execute(statement, dict(name=name)).scalar_one() != 1:
            raise VerificationFailed("advisory_lock_disconnect_failed")
        second.invalidate()
        second.close()
        with engine.connect() as third:
            try:
                if third.execute(statement, dict(name=name)).scalar_one() != 1:
                    raise VerificationFailed("advisory_lock_second_disconnect_failed")
                third.execute(text("SELECT RELEASE_LOCK(:name)"), dict(name=name))
            finally:
                third.invalidate()
    finally:
        # Even a failed verification cannot leave a pooled connection holding
        # its test lock. Disconnect both, never try to unlock another owner.
        if first is not None and not first.closed:
            first.invalidate()
            first.close()
        if second is not None and not second.closed:
            second.invalidate()
            second.close()


def verify(engine, identity, installation_uuid, mode_hold, *, base_dir=None):
    if not isinstance(identity, HostIdentity):
        raise VerificationFailed("host_identity_unverified")
    require_exclusive(mode_hold, installation_uuid, base_dir)
    directory = gate_locks.installation_dir(installation_uuid, base_dir)
    _local_filesystem(directory)
    nonce = uuid.uuid4().hex
    _flock(os.path.join(directory, "verify-" + nonce + ".lock"))
    dialect = engine.dialect.name
    if dialect not in ("sqlite", "mysql", "mariadb"):
        raise VerificationFailed("lock_dialect_unsupported")
    backend = "flock"
    if dialect != "sqlite":
        name = "umv:" + hashlib.sha256((installation_uuid + nonce).encode()).hexdigest()[:56]
        _get_lock(engine, name)
        backend = "flock+get_lock"
    return VerifiedLocks(identity, installation_uuid, backend, dt.datetime.utcnow())
