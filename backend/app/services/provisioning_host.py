"""Host identity and fresh read-only ownership checks for future gate runners.

No ownership claim, takeover, cache, startup hook or mode activation. Missing
identity is not generated implicitly: the installer/explicit setup owns it.
This module imports no ORM or application session and can run in the child.
"""
import hashlib
import os
import re
import stat
import uuid
from dataclasses import dataclass

from sqlalchemy import text


class HostIdentityUnavailable(RuntimeError):
    pass


class OwnershipMismatch(RuntimeError):
    pass


@dataclass(frozen=True)
class HostIdentity:
    host_id: str
    boot_id: str

    def __post_init__(self):
        if not isinstance(self.host_id, str) or not re.fullmatch("[0-9a-f]{64}", self.host_id):
            raise HostIdentityUnavailable("host_identity_invalid")
        try:
            canonical = str(uuid.UUID(self.boot_id))
        except (TypeError, ValueError, AttributeError):
            raise HostIdentityUnavailable("host_identity_invalid") from None
        if canonical != self.boot_id:
            raise HostIdentityUnavailable("host_identity_invalid")


def read_identity(secret_path=None, boot_path="/proc/sys/kernel/random/boot_id"):
    secret_path = secret_path or os.environ.get("UM_HOST_ID_FILE", "/etc/usermanager/host.id")
    descriptor = None
    try:
        # No following links or creating files; fstat closes the stat/open race.
        descriptor = os.open(secret_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or not 32 <= info.st_size <= 4096:
            raise HostIdentityUnavailable("host_identity_invalid")
        secret = os.read(descriptor, 4097)
        if len(secret) != info.st_size:
            raise HostIdentityUnavailable("host_identity_invalid")
        with open(boot_path, "r", encoding="ascii") as source:
            boot = source.read(128).strip()
        return HostIdentity(hashlib.sha256(b"um-host|" + secret).hexdigest(), boot)
    except (OSError, UnicodeError):
        raise HostIdentityUnavailable("host_identity_unavailable") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def validate_snapshot(snapshot, identity, ownership_epoch, installation_uuid):
    if not isinstance(identity, HostIdentity) or type(ownership_epoch) is not int or ownership_epoch < 1:
        raise OwnershipMismatch("ownership_lock_host_mismatch")
    if snapshot is None or any((
        snapshot["installation_uuid"] != installation_uuid,
        snapshot["owner_state"] != "active",
        snapshot["owner_host_id"] != identity.host_id,
        snapshot["owner_boot_id"] != identity.boot_id,
        snapshot["ownership_epoch"] != ownership_epoch,
        snapshot["lock_backend"] not in ("flock", "flock+get_lock"),
    )):
        raise OwnershipMismatch("ownership_lock_host_mismatch")
    return dict(snapshot)


def revalidate(engine, identity, ownership_epoch, installation_uuid):
    """Fresh connection/transaction, never an ORM cache or heartbeat decision."""
    with engine.connect() as connection:
        snapshot = connection.execute(text(
            "SELECT installation_uuid, owner_state, owner_host_id, owner_boot_id, "
            "ownership_epoch, lock_backend, gate_mode, gate_mode_epoch "
            "FROM provisioning_runtime_state WHERE id = 1"
        )).mappings().one_or_none()
        return validate_snapshot(snapshot, identity, ownership_epoch, installation_uuid)
