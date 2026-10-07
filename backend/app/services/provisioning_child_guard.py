"""Private runner hold: file gates + dedicated MariaDB advisory ownership.

Not a transport token, not registered with the runner, and no real node call.
The future action must call check() before EVERY remote writer, including
nested adapter writes. All SELECT transactions end before remote I/O. A lost
DB connection is fatal to this hold; it is never silently re-acquired.
"""
import json
import re

from sqlalchemy import text

from . import gate_locks
from .provisioning_child_database import ChildDatabase
from .provisioning_child_database import ADVISORY_GET, ADVISORY_CHECK, ADVISORY_RELEASE
from .provisioning_dispatch_binding import revalidate
from . import provisioning_contract_rules as contract_rules


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
        self._contract = None

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
            binding = revalidate(self.database, self.binding, self.identity, self.mode_hold, self.node_hold,
                base_dir=self.base_dir)
            with self.database.connect() as connection:
                row = connection.execute(contract_rules.contract_statement(), dict(node_id=binding.node_id,
                    backend=binding.backend)).mappings().one_or_none()
            if row is None or row["state"] != "ready" or row["adapter_version"] != contract_rules.adapter_version() or (
                    row["config_fingerprint"] != contract_rules.endpoint_fingerprint(row)) or not isinstance(
                    row["server_fingerprint"], str) or not re.fullmatch("[0-9a-f]{64}", row["server_fingerprint"]):
                raise ChildGuardUnavailable("child_guard_contract_not_ready")
            contract = contract_rules.validate_contract(row["contract"], binding.backend)
            snapshot = json.dumps(dict(contract=contract, adapter_version=row["adapter_version"],
                server_fingerprint=row["server_fingerprint"], config_fingerprint=row["config_fingerprint"],
                version=row["version"]), sort_keys=True, separators=(",", ":"))
            if self._contract is not None and self._contract != snapshot:
                raise ChildGuardUnavailable("child_guard_contract_changed")
            self._contract = snapshot
            return binding
        except Exception:
            self._broken = True
            self._close()
            raise ChildGuardUnavailable("child_guard_unavailable") from None

    @property
    def contract(self):
        self.check()
        return json.loads(self._contract)  # Detached copy; caller cannot mutate the pinned snapshot.

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
