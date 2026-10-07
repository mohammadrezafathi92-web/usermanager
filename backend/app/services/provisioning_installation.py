"""Fresh read-only DB/file namespace binding (Lifecycle 10.10).

No startup caller, file creation, automatic repair, rotate or fork-reset.
The future control plane must invoke this before granting a gate/runner.
A missing file is NOT guessed from the DB here: off-only bootstrap belongs
to an explicit setup operation. Restored strict installations need recovery.
"""
import os
import stat
import uuid

from sqlalchemy import text

from . import gate_locks


class InstallationUnavailable(RuntimeError):
    pass


def read_file(base_dir=None):
    path = os.path.join(base_dir or gate_locks.lock_base_dir(), "installation.id")
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022 or info.st_size not in (36, 37):
            raise InstallationUnavailable("installation_file_invalid")
        raw = os.read(descriptor, 38)
        if len(raw) != info.st_size:
            raise InstallationUnavailable("installation_file_invalid")
        value = raw.decode("ascii")
        if value.endswith("\n"):
            value = value[:-1]
        if str(uuid.UUID(value)) != value:
            raise InstallationUnavailable("installation_file_invalid")
        return value
    except FileNotFoundError:
        raise InstallationUnavailable("installation_file_missing") from None
    except (OSError, UnicodeError, ValueError):
        raise InstallationUnavailable("installation_file_invalid") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def require_match(engine, *, base_dir=None):
    """SELECT on a fresh connection; never an ORM/cache or readiness grant.

    Failure never rewrites either identity. It blocks even an off runtime
    because a known file/DB mismatch means the lock namespace is unknown.
    """
    identity = read_file(base_dir)
    try:
        with engine.connect() as connection:
            stored = connection.execute(text(
                "SELECT installation_uuid FROM provisioning_runtime_state WHERE id = 1"
            )).scalar_one_or_none()
    except Exception:
        raise InstallationUnavailable("installation_database_unavailable") from None
    if stored is None:
        raise InstallationUnavailable("installation_database_unavailable")
    if stored != identity:
        raise InstallationUnavailable("installation_mismatch")
    return identity
