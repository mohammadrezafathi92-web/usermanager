"""Closed recovery dispatcher: all eight adapters, no write authority/network."""
import ast
import json
import os
import sys
import uuid
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from app.services import provisioning_child_recovery as recovery
from app.services import provisioning_child_authority as authority
from app.services.provisioning_dispatch_binding import DispatchBinding
from app.services.provisioning_host import HostIdentity
from app.services.adapter_base import ReadResult, ReadState
from app.services import remote_action as action


class FakeClient:
    def __init__(self):
        self.closed = False
        self.connected = False
    def connect(self):
        self.connected = True
        return self
    def close(self):
        self.closed = True


mapping = {"mikrotik_wg": (recovery.wg, "read"), "softether": (recovery.se, "read"),
    "xray_ssh": (recovery.xr, "read_ssh"), "threexui": (recovery.xr, "read_threexui"),
    **{backend: (recovery.panels, "read_" + backend) for backend in ("marzban", "hiddify", "marzneshin", "sui")}}
expected = {ReadState.PRESENT_MATCH: action.Outcome.ALREADY_PRESENT_VERIFIED,
    ReadState.ABSENT: action.Outcome.ABSENT_VERIFIED, ReadState.PRESENT_CONFLICT: action.Outcome.CONFLICT,
    ReadState.UNREADABLE: action.Outcome.UNREADABLE}
for backend, (module, name) in mapping.items():
    binding = DispatchBinding(str(uuid.uuid4()), 1, 1, 3, 2, 4, 2, 7, backend, "op:3:1", 1)
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    guard = object.__new__(recovery.ChildGuard)  # Explicit unit fixture; not staging/host proof.
    guard.check = lambda: binding
    guard.identity = host
    guard._contract = json.dumps({"contract": {"not_exist": []}})
    identity = dict.fromkeys(recovery.IDENTITY_FIELDS)
    identity.update(wg_interface="wg0", wg_peer_name="peer", wg_public_key="public", wg_client_address="10.0.0.2/32",
        xr_email="client", xr_inbound_tag="inbound", xr_panel_inbound_id=1, account_username="customer", flow="")
    dto = action.RemoteActionDTO(action_type=action.ActionType.WG_ENSURE_PRESENT if backend == "mikrotik_wg" else (
        action.ActionType.SOFTETHER_ENSURE_PRESENT if backend == "softether" else action.ActionType.XRAY_ENSURE_PRESENT),
        node_id=7, backend=backend, node_config={}, node_secrets=dict.fromkeys(recovery.clients.SECRET_FIELDS, "SECRET"),
        credential={"password": "SECRET", "uuid": str(uuid.uuid4()), "wg_private_key": None}, identity=identity,
        params={"recovery_read": True, "max_concurrent_sessions": 1, "speed_limit_mbps": None}, contract={"not_exist": []},
        fencing={"installation_uuid": binding.installation_uuid, "binding": asdict(binding),
            "host_id": host.host_id, "boot_id": host.boot_id, "expected_parent_pid": os.getppid()})
    with patch.object(authority, "_protected_parent", return_value=True), authority._runner_context(os.getppid()):
        for state in ReadState:
            client = FakeClient()
            with patch.object(recovery.clients, "build", return_value=client) as build, patch.object(module, name,
                    return_value=ReadResult(state, "REMOTE_SECRET", handle={"status": "active", "enable": True,
                        "enabled": True, "secret": "SECRET"})) as read:
                result = recovery.read_present(dto, guard)
                assert result.outcome is (action.Outcome.UNREADABLE if backend in ("softether", "xray_ssh") and
                    state is ReadState.PRESENT_MATCH else expected[state])
                assert result.write_attempted is False and "SECRET" not in result.to_wire()
                assert client.connected and client.closed and build.call_count == read.call_count == 1
                assert read.call_args.args[1].__class__.__name__.endswith("Identity")
                if backend in ("marzban", "hiddify", "marzneshin", "sui"):
                    assert read.call_args.args[2] == []
        def attempts_write(*args):
            authority.require_write(7, backend)
            raise AssertionError("read action obtained writer authority")
        with patch.object(recovery.clients, "build", return_value=FakeClient()), patch.object(module, name,
                side_effect=attempts_write):
            assert recovery.read_present(dto, guard).outcome is action.Outcome.UNREADABLE
        if backend == "mikrotik_wg" or backend in ("marzban", "hiddify", "marzneshin", "sui"):
            uncertain = replace(dto, params={**dto.params, "speed_limit_mbps": 10}) if backend == "mikrotik_wg" else dto
            with patch.object(recovery.clients, "build", return_value=FakeClient()), patch.object(module, name,
                    return_value=ReadResult(ReadState.PRESENT_MATCH, handle={})):  # No positive enabled proof.
                assert recovery.read_present(uncertain, guard).outcome is action.Outcome.UNREADABLE
        lost = [False]
        def fresh_check():
            if lost[0]:
                raise RuntimeError("SECRET")
            return binding
        def read_after_lease_loss(*args):
            lost[0] = True
            return ReadResult(ReadState.ABSENT)
        guard.check = fresh_check
        with patch.object(recovery.clients, "build", return_value=FakeClient()), patch.object(module, name,
                side_effect=read_after_lease_loss):
            assert recovery.read_present(dto, guard).outcome is action.Outcome.UNREADABLE
        guard.check = lambda: binding
        for change in ({"params": {**dto.params, "recovery_read": False}},
                {"fencing": {**dto.fencing, "binding": {**asdict(binding), "step_version": 9}}},
                {"fencing": {**dto.fencing, "binding": {**asdict(binding), "ownership_epoch": True}}},
                {"contract": {"not_exist": [{"foreign": True}]}}, {"node_id": 8},
                {"identity": {**identity, "unexpected": 1}},
                {"fencing": {**dto.fencing, "expected_parent_pid": 0}}):
            with patch.object(recovery.clients, "build") as build:
                try:
                    recovery.read_present(replace(dto, **change), guard)
                    raise AssertionError("invalid recovery descriptor accepted")
                except recovery.RecoveryUnavailable:
                    pass
                build.assert_not_called()
        client = FakeClient()
        def failing_connect():
            raise RuntimeError("SECRET")
        client.connect = failing_connect
        with patch.object(recovery.clients, "build", return_value=client):
            result = recovery.read_present(dto, guard)
            assert result.outcome is action.Outcome.UNREADABLE and client.closed and "SECRET" not in result.to_wire()
    print("PASS", backend, "closed binding, four read outcomes, readonly grant, sanitized failures and close")
tree = ast.parse(Path(recovery.__file__).read_text())
assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and (
    node.func.attr in ("commit", "flush", "run_action", "Popen") or node.func.attr.startswith("ensure_")) for node in ast.walk(tree))
assert _no_network.attempts == []
