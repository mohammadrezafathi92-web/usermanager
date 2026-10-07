"""Fresh DB/file namespace binding and explicit inert-only setup (10.10).

No startup caller, automatic repair, rotate or fork-reset.
The future control plane must invoke this before granting a gate/runner.
A missing file is NOT guessed by the read guard: off-only bootstrap belongs
to initialize_off(). Restored strict installations need recovery.
"""
import os
import stat
import secrets
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


def initialize_off(engine, installation_uuid, mode_hold, *, base_dir=None):
    """Explicit private setup, atomically published and NEVER overwritten.

    Caller keeps mode EX through this call. No DB/mode mutation, and no
    restore/recovery privilege: creation requires the known inert schema.
    A completed file survives a later fsync error; retry verifies it instead
    of rewriting it. Only this attempt's temporary file is cleaned up.
    """
    from sqlalchemy import select
    from .. import models_provisioning as mp
    from . import provisioning_schema as schema
    from .provisioning_lock_verification import require_exclusive
    from .provisioning_transitions import TERMINAL

    require_exclusive(mode_hold, installation_uuid, base_dir)
    with engine.connect() as connection:
        if schema.inspect_schema(connection):
            raise InstallationUnavailable("installation_setup_schema_unverified")
        row = connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one_or_none()
        if row is None or row["installation_uuid"] != installation_uuid:
            raise InstallationUnavailable("installation_mismatch")
        if row["gate_mode"] != "off" or row["owner_state"] not in ("none", "released"):
            raise InstallationUnavailable("installation_setup_not_inert")
        if connection.execute(select(mp.ProvisioningTypeMode.operation_type).where(
                mp.ProvisioningTypeMode.mode != "legacy")).first() or connection.execute(
                select(mp.ProvisioningOperation.id).where(mp.ProvisioningOperation.state.notin_(TERMINAL))).first():
            raise InstallationUnavailable("installation_setup_not_inert")
    directory = descriptor = None
    temporary = ".installation-" + secrets.token_hex(12) + ".tmp"
    owned_temporary = False
    try:
        directory = os.open(base_dir or gate_locks.lock_base_dir(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise InstallationUnavailable("installation_directory_invalid")
        try:
            existing = read_file(base_dir)
        except InstallationUnavailable as exc:
            if str(exc) != "installation_file_missing":
                raise
        else:
            if existing != installation_uuid:
                raise InstallationUnavailable("installation_mismatch")
            os.fsync(directory)
            return {"created": False, "installation_uuid": existing}
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600, dir_fd=directory)
        owned_temporary = True
        os.fchmod(descriptor, 0o600)
        payload = (installation_uuid + "\n").encode("ascii")
        while payload:
            written = os.write(descriptor, payload)
            if written <= 0:
                raise InstallationUnavailable("installation_file_write_failed")
            payload = payload[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, "installation.id", src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
            created = True
        except FileExistsError:
            # Another initializer may have published a COMPLETE file. No
            # replace/rename-over-existing is permitted, even on mismatch.
            created = False
        if read_file(base_dir) != installation_uuid:
            raise InstallationUnavailable("installation_mismatch")
        os.fsync(directory)
        return {"created": created, "installation_uuid": installation_uuid}
    except OSError:
        raise InstallationUnavailable("installation_file_write_failed") from None
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            try:
                if owned_temporary:
                    os.unlink(temporary, dir_fd=directory)
            finally:
                if directory is not None:
                    os.close(directory)
