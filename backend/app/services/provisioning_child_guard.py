"""Private runner hold: file gates + dedicated MariaDB advisory ownership.

Not a transport token, not registered with the runner, and no real node call.
The future action must call check() before EVERY remote writer, including
nested adapter writes. All SELECT transactions end before remote I/O. A lost
DB connection is fatal to this hold; it is never silently re-acquired.
"""
from sqlalchemy import text

from . import gate_locks
from .provisioning_child_database import ChildDatabase
from .provisioning_child_database import ADVISORY_GET, ADVISORY_CHECK, ADVISORY_RELEASE
from .provisioning_dispatch_binding import revalidate


class ChildGuardUnavailable(RuntimeError):
    pass


class ChildGuard:
    def __init__(self, database, binding, identity, mode_hold, node_hold, *, base_dir=None):
        if not isinstance(database, ChildDatabase):
            raise ChildGuardUnavailable("child_guard_database_invalid")
        self.database, self.binding, self.identity = database, binding, identity
        self.mode_hold, self.node_hold, self.base_dir = mode_hold, node_hold, base_dir
        self._connection = None
        self._entered = self._broken = False

    def __enter__(self):
        if self._entered or self._broken:
            raise ChildGuardUnavailable("child_guard_not_reusable")
        try:
            # Check file locks and a committed binding before acquiring an
            # advisory lock; check again afterwards to close the wait window.
            revalidate(self.database, self.binding, self.identity, self.mode_hold, self.node_hold,
                base_dir=self.base_dir)
            if self.database.dialect.name in ("mysql", "mariadb"):
                self._connection = self.database.connect()
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                if self._connection.execute(text(ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() != 1:
                    raise ChildGuardUnavailable("child_guard_advisory_unavailable")
                self._connection.end_snapshot()
            self._entered = True
            self.check()
            return self
        except Exception:
            self._broken = True
            self._close()
            raise ChildGuardUnavailable("child_guard_unavailable") from None

    def check(self):
        if not self._entered or self._broken:
            raise ChildGuardUnavailable("child_guard_unavailable")
        try:
            if self._connection is not None:
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                if self._connection.execute(text(ADVISORY_CHECK), dict(name=name)).scalar_one() != 1:
                    raise ChildGuardUnavailable("child_guard_advisory_lost")
                self._connection.end_snapshot()
            return revalidate(self.database, self.binding, self.identity, self.mode_hold, self.node_hold,
                base_dir=self.base_dir)
        except Exception:
            self._broken = True
            self._close()
            raise ChildGuardUnavailable("child_guard_unavailable") from None

    def _close(self):
        if self._connection is not None:
            connection, self._connection = self._connection, None
            try:
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                connection.execute(text(ADVISORY_RELEASE), dict(name=name))
            except Exception:
                pass  # Never reconnect/re-acquire; physical close releases this session's lock.
            finally:
                try:
                    connection.close()
                except Exception:
                    pass  # Error reporting must not expose a driver URL/credential.

    def __exit__(self, *exception):
        self._broken = True
        self._close()
