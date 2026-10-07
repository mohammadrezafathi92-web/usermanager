"""Private guarded forward/convergence action; unregistered, no live caller.

Uses the same stored identity for initial creation and safe retries. Writers
revalidate authority at the transport. A live DB read supplies the current
WireGuard subnet AFTER acquiring the node gate; no DB transaction spans I/O.
"""
from dataclasses import replace

from sqlalchemy import text

from . import remote_action
from . import provisioning_child_recovery as recovery, provisioning_child_authority as authority
from . import provisioning_child_clients as clients
from .provisioning_child_database import READ_SUBNET
from .adapter_base import PresentResult, AdapterConflict


def _attempted(client, backend):
    if client is None:
        return False
    if backend == "softether":
        return client.write_attempted
    transport = (getattr(client, "_api", None) if backend == "mikrotik_wg" else
        getattr(client, "_client", None) if backend == "xray_ssh" else getattr(client, "session", None))
    return getattr(transport, "write_attempted", False) is True


def _ensure(client, dto, guard):
    identity = recovery._identity(dto)
    if dto.backend == "mikrotik_wg":
        guard.check()
        with guard.database.connect() as connection:
            subnet = connection.execute(text(READ_SUBNET), {"node_id": dto.node_id}).scalar_one()
        guard.check()
        identity = replace(identity, speed_limit_mbps=dto.params["speed_limit_mbps"])
        return recovery.wg.ensure_present(client, identity, subnet)
    if dto.backend == "softether":
        return recovery.se.ensure_present(client, identity)
    if dto.backend in ("xray_ssh", "threexui"):
        return (recovery.xr.ensure_present_ssh(client, identity, confirm_restart=True) if dto.backend == "xray_ssh" else
            recovery.xr.ensure_present_threexui(client, identity))
    ensures = {"marzban": recovery.panels.ensure_present_marzban, "hiddify": recovery.panels.ensure_present_hiddify,
        "marzneshin": recovery.panels.ensure_present_marzneshin, "sui": recovery.panels.ensure_present_sui}
    return ensures[dto.backend](client, identity, dto.contract["not_exist"])


def ensure_present(dto, guard):
    recovery._validate(dto, guard, recovery_read=False)
    client = None
    attempted = False
    outcome = remote_action.Outcome.TRANSPORT_ERROR
    error = "remote_transport_error"
    try:
        with authority._scope(guard, allow_writes=True):
            try:
                client = clients.build(guard, dto.node_config, dto.node_secrets)
                client.connect()
                try:
                    found = _ensure(client, dto, guard)
                except AdapterConflict:
                    attempted = _attempted(client, dto.backend)
                    if not attempted:
                        outcome, error = remote_action.Outcome.CONFLICT, "remote_identity_conflict"
                    raise
                attempted = _attempted(client, dto.backend)
                guard.check()
                if type(found) is not PresentResult or type(found.created) is not bool or (found.created and not attempted):
                    raise RuntimeError("child_present_unconfirmed")
                outcome = remote_action.Outcome.SUCCEEDED if attempted else remote_action.Outcome.ALREADY_PRESENT_VERIFIED
                error = None
            finally:
                if client is not None:
                    attempted = attempted or _attempted(client, dto.backend)
                    client.close()
    except Exception:
        # A close/ownership/transport failure cannot retain a success result.
        if outcome not in (remote_action.Outcome.CONFLICT, remote_action.Outcome.TRANSPORT_ERROR):
            outcome, error = remote_action.Outcome.TRANSPORT_ERROR, "remote_transport_error"
    return remote_action.RemoteActionResult(action_id=dto.action_id, outcome=outcome,
        write_attempted=attempted, error_code=error)
