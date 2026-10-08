"""Guarded child entry for stored forward actions; no live worker caller.

Self-test imports remain database-free. Real actions obtain their read-only
DB URL from trusted process configuration, NEVER DTO fields. Mode/node locks
are supplied by the runner's own context, not by action parameters. Missing
Linux parent protection, external host identity, configuration, namespace,
current ownership/leases/contract or enabled durable mode refuses all I/O.
"""
import contextlib
import contextvars
import os
import threading

from . import gate_locks
from . import provisioning_child_authority as authority

_holds = contextvars.ContextVar("provisioning_runner_holds", default=None)


class ChildEntryUnavailable(RuntimeError):
    pass


@contextlib.contextmanager
def locked_context(mode_hold, node_hold):
    authority._role()
    if type(mode_hold) is not gate_locks.FileLock or type(node_hold) is not gate_locks.FileLock:
        raise ChildEntryUnavailable("child_entry_locks_missing")
    mark = _holds.set((os.getpid(), threading.get_ident(), mode_hold, node_hold))
    try:
        yield
    finally:
        _holds.reset(mark)


def forward(dto):
    return _run(dto, phase="forward")


def compensate(dto):
    """Guarded runner entry for the exact stored cleanup identity."""
    return _run(dto, phase="compensation")


def absent(dto):
    """Route the shared ensure-absent action by its sealed state-machine phase."""
    try:
        phase = dto.fencing["binding"]["phase"]
    except Exception:
        raise ChildEntryUnavailable("child_entry_phase_invalid") from None
    if phase not in ("compensation", "deletion"):
        raise ChildEntryUnavailable("child_entry_phase_invalid")
    return _run(dto, phase=phase)


def _run(dto, *, phase):
    if phase not in ("forward", "compensation", "deletion"):
        raise ChildEntryUnavailable("child_entry_phase_invalid")
    authority._role()  # Linux kernel protection before DB access or client imports.
    holds = _holds.get()
    if holds is None or holds[:2] != (os.getpid(), threading.get_ident()):
        raise ChildEntryUnavailable("child_entry_locks_missing")
    trusted_url = os.environ.get("DATABASE_URL", "").strip()
    if not trusted_url:
        raise ChildEntryUnavailable("child_entry_configuration_missing")
    from .provisioning_child_database import ChildDatabase
    from .provisioning_child_guard import ChildGuard
    from .provisioning_dispatch_binding import DispatchBinding
    from .provisioning_host import read_identity
    from . import provisioning_child_recovery as recovery, provisioning_child_present as present
    database = None
    try:
        binding = DispatchBinding(**dto.fencing["binding"])
        identity = read_identity()
        database = ChildDatabase(trusted_url)
        with ChildGuard(database, binding, identity, holds[2], holds[3]) as guard:
            if phase in ("compensation", "deletion"):
                from . import provisioning_child_absent as absent
                return absent.ensure_absent(dto, guard, phase=phase)
            if dto.params.get("recovery_read") is True:
                return recovery.read_present(dto, guard)
            return present.ensure_present(dto, guard)
    except Exception:
        raise ChildEntryUnavailable("child_entry_unavailable") from None
    finally:
        if database is not None:
            try:
                database.dispose()
            except Exception:
                raise ChildEntryUnavailable("child_entry_cleanup_unavailable") from None
