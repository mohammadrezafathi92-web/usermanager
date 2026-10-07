"""Authority lifecycle unit tests; mocked proof is NOT deployment readiness."""
import ast
import contextvars
import os
import sys
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.services import provisioning_child_authority as authority
from app.services.provisioning_child_guard import ChildGuard
from app.services.provisioning_dispatch_binding import DispatchBinding
from app.services.remote_runner_child import arm_parent_death_signal


def refused(callback):
    try:
        callback()
        raise AssertionError("invalid authority accepted")
    except authority.WriteAuthorityUnavailable:
        pass


binding = DispatchBinding(str(uuid.uuid4()), 1, 1, 1, 0, 1, 0, 1, "xray_ssh", "op:1:1", 1)
guard = object.__new__(ChildGuard)
checks = []
def check():
    checks.append(True)
    return binding
guard.check = check  # Explicit isolated lifecycle fixture; real DB guard is tested separately.
refused(lambda: authority.require_write(1, "xray_ssh"))
with patch.object(authority.os, "getppid", return_value=1):
    with authority._runner_context(1):  # Backend can be PID 1 in a container.
        assert authority._runner.get()[2] == 1
for bad in (0, -1, True, "1"):
    refused(lambda bad=bad: authority._runner_context(bad).__enter__())
with authority._runner_context(os.getppid()):
    with patch.object(authority, "_protected_parent", return_value=False):
        refused(lambda: authority._scope(guard).__enter__())
    with patch.object(authority, "_protected_parent", return_value=True):
        refused(lambda: authority._scope(object()).__enter__())
        with authority._scope(guard) as token:
            initial = len(checks)
            authority.require_write(1, "xray_ssh")
            authority.require_write(1, "xray_ssh")
            assert len(checks) == initial + 2  # Every writer, including nested writes, revalidates.
            authority._require_guard(guard)
            refused(lambda: authority._require_guard(object.__new__(ChildGuard)))
            for node, backend in ((None, "xray_ssh"), (True, "xray_ssh"), (2, "xray_ssh"), (1, "softether")):
                refused(lambda node=node, backend=backend: authority.require_write(node, backend))
            with authority._scope(guard) as nested:
                assert nested is token
            forged = replace(token)
            mark = authority._current.set(forged)
            try:
                refused(lambda: authority.require_write(1, "xray_ssh"))
            finally:
                authority._current.reset(mark)
            copied = contextvars.copy_context()
            thread_errors = []
            def copied_thread():
                try:
                    copied.run(authority.require_write, 1, "xray_ssh")
                    thread_errors.append("cross-thread authority accepted")
                except authority.WriteAuthorityUnavailable:
                    pass
                def fresh_role_old_token():
                    with authority._runner_context(os.getppid()):
                        refused(lambda: authority._require_guard(guard))
                copied.run(fresh_role_old_token)
            thread = threading.Thread(target=copied_thread)
            thread.start()
            thread.join(timeout=3)
            assert not thread.is_alive() and thread_errors == []
            with patch.object(authority.os, "getpid", return_value=token.pid + 1):
                refused(lambda: authority.require_write(1, "xray_ssh"))
                refused(lambda: authority._require_guard(guard))
            with patch.object(authority, "_protected_parent", return_value=False):
                refused(lambda: authority.require_write(1, "xray_ssh"))
            with patch.object(guard, "check", return_value=replace(binding, ownership_epoch=2)):
                refused(lambda: authority.require_write(1, "xray_ssh"))
            with patch.object(guard, "check", side_effect=RuntimeError("SECRET_SENTINEL")):
                try:
                    authority.require_write(1, "xray_ssh")
                    raise AssertionError("lost guard accepted")
                except authority.WriteAuthorityUnavailable as exc:
                    assert "SECRET_SENTINEL" not in str(exc)
        assert authority._registry == {}
        refused(lambda: copied.run(authority.require_write, 1, "xray_ssh"))
        try:
            with authority._scope(guard):
                raise RuntimeError("injected action failure")
        except RuntimeError:
            pass
        assert authority._registry == {} and authority._current.get() is None
        with authority._scope(guard, allow_writes=False) as read_token:
            authority._require_guard(guard)  # Snapshot-bound factory is allowed, writes are not.
            refused(lambda: authority.require_write(1, "xray_ssh"))
            refused(lambda: authority._scope(guard, allow_writes=True).__enter__())
            with authority._scope(guard, allow_writes=False) as nested:
                assert nested is read_token
            with patch.object(guard, "check", return_value=replace(binding, ownership_epoch=2)):
                refused(lambda: authority._require_guard(guard))
        refused(lambda: authority._scope(guard, allow_writes=1).__enter__())
        assert authority._registry == {}
assert authority._runner.get() is None
if sys.platform.startswith("linux"):
    assert arm_parent_death_signal()
    assert authority._protected_parent(os.getppid())
    assert not authority._protected_parent(os.getppid() + 1)
    print("PASS Linux real parent-death signal read-back protects writer authority")
else:
    assert not authority._protected_parent(os.getppid())
tree = ast.parse(Path(authority.__file__).read_text())
assert not any(isinstance(n, ast.ImportFrom) and n.module and (
    "models" in n.module or n.module in ("database", "config", "sqlalchemy.orm")) for n in ast.walk(tree))
print("PASS child authority: per-write proof, pid/thread/node binding, nesting, forged/stale tokens, parent loss and cleanup")
