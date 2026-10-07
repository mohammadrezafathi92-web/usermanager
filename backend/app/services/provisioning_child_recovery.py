"""Private read-only forward recovery through the eight guarded clients.

No runner registration or live caller. The runner must supply an entered
ChildGuard from trusted configuration, not a database URL from the DTO.
No create/delete/enable call is allowed, even inside a misbehaving read.
"""
from dataclasses import asdict
import os

from . import remote_action
from . import provisioning_child_authority as authority
from . import provisioning_child_clients as clients
from . import adapter_mikrotik_wg as wg, adapter_softether as se
from . import adapter_xray as xr, adapter_xray_panels as panels
from .adapter_base import ReadResult, ReadState
from .provisioning_child_guard import ChildGuard
from .provisioning_dispatch_binding import DispatchBinding

IDENTITY_FIELDS = frozenset(("wg_interface", "wg_peer_name", "wg_public_key", "wg_client_address",
    "wg_gateway_with_prefix", "xr_inbound_tag", "xr_panel_inbound_id", "xr_email", "account_username", "flow"))
CREDENTIAL_FIELDS = frozenset(("wg_private_key", "password", "uuid"))


class RecoveryUnavailable(RuntimeError):
    pass


def _validate(dto, guard, *, recovery_read=True):
    if type(dto) is not remote_action.RemoteActionDTO or type(guard) is not ChildGuard:
        raise RecoveryUnavailable("child_recovery_invalid")
    binding = guard.check()
    try:
        supplied = DispatchBinding(**dto.fencing["binding"])
        remote_action.RemoteActionDTO.from_wire(dto.to_wire())
    except Exception:
        raise RecoveryUnavailable("child_recovery_binding_invalid") from None
    expected_action = (remote_action.ActionType.WG_ENSURE_PRESENT if binding.backend == "mikrotik_wg" else
        remote_action.ActionType.SOFTETHER_ENSURE_PRESENT if binding.backend == "softether" else
        remote_action.ActionType.XRAY_ENSURE_PRESENT)
    if binding.phase != "forward" or dto.action_type != expected_action or (
            dto.node_id, dto.backend) != (binding.node_id, binding.backend) or (
            set(dto.fencing) != {"installation_uuid", "binding", "host_id", "boot_id", "expected_parent_pid"}) or (
            supplied != binding or dto.fencing["binding"] != asdict(binding) or dto.fencing["installation_uuid"] != binding.installation_uuid) or (
            type(dto.fencing["expected_parent_pid"]) is not int or dto.fencing["expected_parent_pid"] != os.getppid()) or (
            dto.fencing["host_id"], dto.fencing["boot_id"]) != (guard.identity.host_id, guard.identity.boot_id) or (
            set(dto.params) != {"recovery_read", "max_concurrent_sessions", "speed_limit_mbps"}) or (
            type(recovery_read) is not bool or dto.params["recovery_read"] is not recovery_read) or set(dto.identity) != IDENTITY_FIELDS or (
            set(dto.credential) != CREDENTIAL_FIELDS) or set(dto.node_secrets) != clients.SECRET_FIELDS or (
            dto.contract != guard.contract["contract"]):
        raise RecoveryUnavailable("child_recovery_binding_invalid")
    return binding


def _identity(dto):
    i, c, backend = dto.identity, dto.credential, dto.backend
    if backend == "mikrotik_wg":
        identity = wg.WireguardIdentity(i["wg_interface"], i["wg_peer_name"], i["wg_public_key"], i["wg_client_address"])
        return identity
    if backend == "softether":
        return se.SoftEtherIdentity(i["account_username"], c["password"])
    values = dict(email=i["xr_email"], uuid=c["uuid"], flow=i["flow"] or "",
                  inbound_tag=i["xr_inbound_tag"], inbound_id=i["xr_panel_inbound_id"])
    if backend in ("xray_ssh", "threexui"):
        identity = xr.XrayIdentity(**values)
        return identity
    return panels.PanelIdentity(**values, username=i["account_username"])


def _read(client, dto):
    identity = _identity(dto)
    if dto.backend == "mikrotik_wg":
        return wg.read(client, identity)
    if dto.backend == "softether":
        return se.read(client, identity)
    if dto.backend in ("xray_ssh", "threexui"):
        return (xr.read_ssh if dto.backend == "xray_ssh" else xr.read_threexui)(client, identity)
    reads = {"marzban": panels.read_marzban, "hiddify": panels.read_hiddify,
             "marzneshin": panels.read_marzneshin, "sui": panels.read_sui}
    return reads[dto.backend](client, identity, dto.contract["not_exist"])


def read_present(dto, guard):
    _validate(dto, guard)  # Refuse wrong descriptors BEFORE constructing/authenticating a client.
    client = None
    needs_convergence = False
    try:
        with authority._scope(guard, allow_writes=False):
            try:
                client = clients.build(guard, dto.node_config, dto.node_secrets)
                client.connect()
                found = _read(client, dto)
                guard.check()  # Lost ownership/contract/lease cannot certify a stale read.
                if type(found) is not ReadResult or type(found.state) is not ReadState:
                    raise RecoveryUnavailable("child_recovery_result_invalid")
                if found.state is ReadState.PRESENT_MATCH:
                    # Presence cannot certify password convergence, an SSH
                    # config's successful service restart, or a speed queue
                    # installed AFTER peer creation. Separate convergence is
                    # needed; never mark these ambiguous side effects complete.
                    unconfirmed = dto.backend in ("softether", "xray_ssh") or (
                        dto.backend == "mikrotik_wg" and bool(dto.params["speed_limit_mbps"]))
                    if dto.backend in ("marzban", "hiddify", "marzneshin", "sui"):
                        record = found.handle
                        field = {"marzban": "status", "hiddify": "enable", "marzneshin": "enabled", "sui": "enable"}[dto.backend]
                        enabled = isinstance(record, dict) and (record.get(field) == "active" if dto.backend == "marzban" else
                            record.get(field) is True)
                        unconfirmed = unconfirmed or not enabled
                    if unconfirmed:
                        needs_convergence = True
                        found = ReadResult(ReadState.UNREADABLE)
            finally:
                if client is not None:
                    client.close()
        outcomes = {ReadState.PRESENT_MATCH: remote_action.Outcome.ALREADY_PRESENT_VERIFIED,
            ReadState.ABSENT: remote_action.Outcome.ABSENT_VERIFIED,
            ReadState.PRESENT_CONFLICT: remote_action.Outcome.CONFLICT,
            ReadState.UNREADABLE: remote_action.Outcome.UNREADABLE}
        return remote_action.RemoteActionResult(action_id=dto.action_id, outcome=outcomes[found.state],
            write_attempted=False, remote_outcome="verified_absent" if found.state is ReadState.ABSENT else None,
            error_code="remote_convergence_required" if needs_convergence else "remote_unreadable" if found.state is ReadState.UNREADABLE else (
                "remote_identity_conflict" if found.state is ReadState.PRESENT_CONFLICT else None))
    except Exception:
        # Connection/read/close failures never prove absence. Do not copy
        # exceptions, remote payloads, handles or customer secrets into logs.
        return remote_action.RemoteActionResult(action_id=dto.action_id,
            outcome=remote_action.Outcome.UNREADABLE, write_attempted=False, error_code="remote_unreadable")
