"""Private compensation unit fixtures: no real runner, node or mode changes."""
import json
import os
import sys
import uuid
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from app.services import provisioning_child_absent as absent, provisioning_child_recovery as recovery
from app.services import provisioning_child_authority as authority, remote_action as action
from app.services.provisioning_dispatch_binding import DispatchBinding
from app.services.provisioning_host import HostIdentity
from app.services.adapter_base import AbsentOutcome, AdapterConflict


class Client:
    def __init__(self):
        self.closed = False
        self._api = self._client = self.session = SimpleNamespace(write_attempted=False)
    @property
    def write_attempted(self):
        return self.session.write_attempted
    def connect(self):
        return self
    def close(self):
        self.closed = True


mapping = {"mikrotik_wg": (recovery.wg, "ensure_absent"), "softether": (recovery.se, "ensure_absent"),
    "xray_ssh": (recovery.xr, "ensure_absent_ssh"), "threexui": (recovery.xr, "ensure_absent_threexui"),
    **{name: (recovery.panels, "ensure_absent_" + name) for name in ("marzban", "hiddify", "marzneshin", "sui")}}
for backend, (module, method) in mapping.items():
    binding = DispatchBinding(str(uuid.uuid4()), 1, 1, 3, 2, 4, 2, 7, backend, "op:3:1", 1, "compensation")
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    guard = object.__new__(recovery.ChildGuard)  # Deliberate unit double, not physical host proof.
    guard.check = lambda: binding
    guard.identity = host
    matcher = {"kind": "jsonrpc", "op": "delete", "error_code": 99} if backend == "softether" else None
    contract = {"not_exist": [matcher] if matcher else []}
    guard._contract = json.dumps({"contract": contract})
    identity = dict.fromkeys(recovery.IDENTITY_FIELDS)
    identity.update(wg_interface="wg0", wg_peer_name="peer", wg_public_key="public", wg_client_address="10.0.0.2/32",
        xr_email="client", xr_inbound_tag="inbound", xr_panel_inbound_id=1, account_username="customer", flow="")
    dto = action.RemoteActionDTO(action_type=action.ActionType.WG_ENSURE_ABSENT if backend == "mikrotik_wg" else (
        action.ActionType.SOFTETHER_ENSURE_ABSENT if backend == "softether" else action.ActionType.XRAY_ENSURE_ABSENT),
        node_id=7, backend=backend, node_config={}, node_secrets=dict.fromkeys(recovery.clients.SECRET_FIELDS, "SECRET"),
        credential={"password": "SECRET", "uuid": str(uuid.uuid4()), "wg_private_key": None}, identity=identity,
        params={"recovery_read": False, "max_concurrent_sessions": 1, "speed_limit_mbps": None}, contract=contract,
        fencing={"installation_uuid": binding.installation_uuid, "binding": asdict(binding),
            "host_id": host.host_id, "boot_id": host.boot_id, "expected_parent_pid": os.getppid()})
    with patch.object(authority, "_protected_parent", return_value=True), authority._runner_context(os.getppid()):
        for found in AbsentOutcome:
            for wrote in (False, True):
                client = Client()
                def remove(*args, **kwargs):
                    assert args[1].__class__.__name__.endswith("Identity")
                    if backend == "softether":
                        assert args[2] == [99]
                    if backend == "xray_ssh":
                        assert kwargs == {"confirm_restart": True}
                    if wrote:
                        authority.require_write(7, backend)
                        client.session.write_attempted = True
                    return found
                with patch.object(absent.clients, "build", return_value=client), patch.object(module, method, side_effect=remove):
                    result = absent.ensure_absent(dto, guard)
                assert client.closed and result.write_attempted is wrote
                if found is AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT and not wrote:
                    assert result.outcome is action.Outcome.TRANSPORT_ERROR and result.remote_outcome is None
                elif found is AbsentOutcome.UNVERIFIED:
                    assert result.outcome is (action.Outcome.TRANSPORT_ERROR if wrote else action.Outcome.UNREADABLE)
                    assert result.remote_outcome is None
                else:
                    assert result.outcome is action.Outcome.ABSENT_VERIFIED and result.remote_outcome == found.value
                assert "SECRET" not in result.to_wire()
        for failure in ("identity", "partial", "lease", "close", "bad_result"):
            client = Client()
            lost = [False]
            def check():
                if lost[0]:
                    raise RuntimeError("SECRET")
                return binding
            guard.check = check
            def broken(*args, **kwargs):
                if failure == "identity":
                    raise AdapterConflict("SECRET")
                authority.require_write(7, backend)
                client.session.write_attempted = True
                if failure == "partial":
                    raise AdapterConflict("SECRET")
                if failure == "lease":
                    lost[0] = True
                return "verified_absent" if failure == "bad_result" else AbsentOutcome.VERIFIED_ABSENT
            if failure == "close":
                client.close = lambda: (_ for _ in ()).throw(RuntimeError("SECRET"))
            with patch.object(absent.clients, "build", return_value=client), patch.object(module, method, side_effect=broken):
                result = absent.ensure_absent(dto, guard)
            assert result.outcome is (action.Outcome.CONFLICT if failure == "identity" else action.Outcome.TRANSPORT_ERROR)
            assert result.remote_outcome in (None, "unverified") and "SECRET" not in result.to_wire()
        guard.check = lambda: binding
        for changed in (replace(dto, params={**dto.params, "recovery_read": True}),
                replace(dto, action_type=action.ActionType.WG_ENSURE_PRESENT),
                replace(dto, fencing={**dto.fencing, "binding": {**asdict(binding), "phase": "forward"}})):
            with patch.object(absent.clients, "build") as build:
                try:
                    absent.ensure_absent(changed, guard)
                    raise AssertionError("wrong compensation descriptor accepted")
                except recovery.RecoveryUnavailable:
                    pass
                build.assert_not_called()
    print("PASS", backend, "verified absence, partial writes, stale authority, closure and wrong-phase refusal")

# An absent SSH config may precede a missed restart. Opt-in cleanup must
# converge the running service; default legacy semantics remain unchanged.
for fail in (False, True):
    client = SimpleNamespace(get_inbound_clients_strict=lambda _: [], restart_service=lambda: None)
    with patch.object(client, "restart_service", side_effect=RuntimeError("SECRET") if fail else None) as restart:
        found = recovery.xr.ensure_absent_ssh(client, recovery.xr.XrayIdentity("client", str(uuid.uuid4()), inbound_tag="inbound"),
            confirm_restart=True)
        assert found is (AbsentOutcome.UNVERIFIED if fail else AbsentOutcome.VERIFIED_ABSENT)
        restart.assert_called_once()
assert _no_network.attempts == []
