"""Guarded compensation helper; no live scheduler/API caller.

Only the stored compensation identity may be removed. An adapter's verified
absence is accepted only after fresh authority and successful client close.
Ambiguous writes never certify absence or authorize reservation release.
"""
from . import remote_action
from . import provisioning_child_recovery as recovery, provisioning_child_authority as authority
from . import provisioning_child_clients as clients, provisioning_child_present as present
from .adapter_base import AbsentOutcome, AdapterConflict


def _ensure(client, dto):
    identity = recovery._identity(dto)
    if dto.backend == "mikrotik_wg":
        return recovery.wg.ensure_absent(client, identity)
    if dto.backend == "softether":
        codes = [item["error_code"] for item in dto.contract["not_exist"]
                 if item["kind"] == "jsonrpc" and item["op"] == "delete"]
        return recovery.se.ensure_absent(client, identity, codes)
    if dto.backend == "xray_ssh":
        return recovery.xr.ensure_absent_ssh(client, identity, confirm_restart=True)
    if dto.backend == "threexui":
        return recovery.xr.ensure_absent_threexui(client, identity)
    ensures = {"marzban": recovery.panels.ensure_absent_marzban, "hiddify": recovery.panels.ensure_absent_hiddify,
        "marzneshin": recovery.panels.ensure_absent_marzneshin, "sui": recovery.panels.ensure_absent_sui}
    return ensures[dto.backend](client, identity, dto.contract["not_exist"])


def ensure_absent(dto, guard):
    recovery._validate(dto, guard, recovery_read=False, phase="compensation")
    client = None
    attempted = False
    outcome, error, remote = remote_action.Outcome.TRANSPORT_ERROR, "remote_transport_error", None
    try:
        with authority._scope(guard, allow_writes=True):
            try:
                client = clients.build(guard, dto.node_config, dto.node_secrets)
                client.connect()
                found = _ensure(client, dto)
                attempted = present._attempted(client, dto.backend)
                guard.check()
                if type(found) is not AbsentOutcome:
                    raise RuntimeError("child_absence_unconfirmed")
                if found is AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT and not attempted:
                    raise RuntimeError("child_delete_not_observed")
                if found is not AbsentOutcome.UNVERIFIED:
                    outcome, error, remote = remote_action.Outcome.ABSENT_VERIFIED, None, found.value
                elif not attempted:
                    outcome, error = remote_action.Outcome.UNREADABLE, "remote_absence_unverified"
            finally:
                if client is not None:
                    attempted = attempted or present._attempted(client, dto.backend)
                    client.close()
    except AdapterConflict:
        if not attempted:
            outcome, error, remote = remote_action.Outcome.CONFLICT, "remote_identity_conflict", "unverified"
        else:
            outcome, error, remote = remote_action.Outcome.TRANSPORT_ERROR, "remote_transport_error", None
    except Exception:
        outcome, error, remote = remote_action.Outcome.TRANSPORT_ERROR, "remote_transport_error", None
    return remote_action.RemoteActionResult(dto.action_id, outcome, write_attempted=attempted,
        remote_outcome=remote, error_code=error)
