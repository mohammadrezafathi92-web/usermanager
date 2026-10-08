"""Guarded runner entry has no DTO-sourced DB config or unbound execution."""
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, replace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from app.services import provisioning_child_entry as entry, provisioning_child_authority as authority
from app.services import provisioning_child_database as database, provisioning_child_guard as guards
from app.services import provisioning_host as host, provisioning_child_recovery as recovery, provisioning_child_present as present
from app.services import provisioning_child_absent as absent
from app.services import gate_locks, remote_action as action
from app.services.provisioning_dispatch_binding import DispatchBinding


def refused(callback):
    try:
        callback()
        raise AssertionError("unbound runner entry accepted")
    except (entry.ChildEntryUnavailable, authority.WriteAuthorityUnavailable) as error:
        assert "SECRET" not in str(error)


binding = DispatchBinding(str(uuid.uuid4()), 1, 1, 3, 0, 4, 0, 7, "threexui", "op:3:1", 1)
identity = host.HostIdentity("a" * 64, str(uuid.uuid4()))
dto = action.RemoteActionDTO(action_type=action.ActionType.XRAY_ENSURE_PRESENT, node_id=7, backend="threexui",
    fencing={"binding": asdict(binding)}, params={"recovery_read": True, "database_url": "UNTRUSTED_SECRET"})
refused(lambda: entry.forward(dto))  # No actual Linux runner role.
with patch.object(authority, "_protected_parent", return_value=True), authority._runner_context(os.getppid()):
    refused(lambda: entry.forward(dto))  # Even a role cannot invent held locks.
    with tempfile.TemporaryDirectory(prefix="um-child-entry-") as directory:
        mode = gate_locks.FileLock(gate_locks.mode_lock_path(binding.installation_uuid, directory)).acquire(shared=True, timeout=0)
        node = gate_locks.FileLock(gate_locks.node_lock_path(binding.installation_uuid, 7, directory)).acquire(shared=False, timeout=0)
        try:
            with entry.locked_context(mode, node):
                with patch.dict(os.environ, {"DATABASE_URL": ""}), patch.object(database, "ChildDatabase") as constructor:
                    refused(lambda: entry.forward(dto))
                    constructor.assert_not_called()
                with patch.dict(os.environ, {"DATABASE_URL": "TRUSTED_SECRET"}), patch.object(host, "read_identity", return_value=identity), (
                        patch.object(database, "ChildDatabase", side_effect=RuntimeError("SECRET"))) as constructor:
                    refused(lambda: entry.forward(dto))
                    assert constructor.call_args.args == ("TRUSTED_SECRET",)  # DTO config never selected.
                guard = object.__new__(guards.ChildGuard)
                guard.check = lambda: binding
                guard.identity = identity
                guard._contract = json.dumps({"contract": {"not_exist": []}})
                # Explicit entry wiring unit doubles; concrete guard/DB proof
                # is independently exercised on SQLite and real MariaDB in CI.
                with patch.dict(os.environ, {"DATABASE_URL": "TRUSTED_SECRET"}), patch.object(host, "read_identity", return_value=identity), (
                        patch.object(database, "ChildDatabase")) as constructor, patch.object(guards.ChildGuard, "__init__", return_value=None), (
                        patch.object(guards.ChildGuard, "__enter__", return_value=guard)), patch.object(guards.ChildGuard, "__exit__", return_value=False):
                    expected = action.RemoteActionResult(dto.action_id, action.Outcome.UNREADABLE, write_attempted=False)
                    with patch.object(recovery, "read_present", return_value=expected) as read, patch.object(present, "ensure_present") as write:
                        assert entry.forward(dto) is expected
                        read.assert_called_once_with(dto, guard)
                        write.assert_not_called()
                    assert constructor.return_value.dispose.call_count == 1
                    with patch.object(present, "ensure_present", return_value=expected) as write, patch.object(recovery, "read_present") as read:
                        forward = replace(dto, params={"recovery_read": False})
                        assert entry.forward(forward) is expected
                        write.assert_called_once_with(forward, guard)
                        read.assert_not_called()
                    assert constructor.return_value.dispose.call_count == 2
                    cleanup = replace(dto, action_type=action.ActionType.XRAY_ENSURE_ABSENT,
                        fencing={"binding": asdict(replace(binding, phase="compensation"))}, params={"recovery_read": False})
                    with patch.object(absent, "ensure_absent", return_value=expected) as remove, (
                            patch.object(recovery, "read_present")) as read, patch.object(present, "ensure_present") as write:
                        assert entry.compensate(cleanup) is expected
                        remove.assert_called_once_with(cleanup, guard, phase="compensation")
                        read.assert_not_called()
                        write.assert_not_called()
                    assert constructor.return_value.dispose.call_count == 3
                    deletion = replace(cleanup,
                        fencing={"binding": asdict(replace(binding, phase="deletion"))})
                    with patch.object(absent, "ensure_absent", return_value=expected) as remove:
                        assert entry.absent(deletion) is expected
                        remove.assert_called_once_with(deletion, guard, phase="deletion")
                    assert constructor.return_value.dispose.call_count == 4
                    constructor.return_value.dispose.side_effect = RuntimeError("SECRET")
                    with patch.object(recovery, "read_present", return_value=expected):
                        refused(lambda: entry.forward(dto))
            refused(lambda: entry.forward(dto))  # Context cannot escape its lifetime.
        finally:
            node.release()
            mode.release()
assert _no_network.attempts == []
print("PASS guarded child entry: role/locks, trusted configuration only, sanitized refusal, read-only routing and disposal")
