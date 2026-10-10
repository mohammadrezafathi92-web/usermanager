"""Parent-owned forward/compensation snapshots; no spawn, commit or live caller.

Caller owns resource leases and a short fenced transaction. A first send
marks remote_calling in that same transaction; retries of remote_calling
are read-only descriptors. Commit BEFORE spawning the child. Management
and customer credentials come only from the locked DB rows, not a request.
Approval-correlated operations remain unavailable until full integration.
"""
import datetime as dt
from dataclasses import asdict
import os
import json
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import resource_leases, provisioning_schema, provisioning_transitions as transitions
from . import provisioning_contracts as contracts, provisioning_contract_rules as rules
from . import remote_action
from . import wallet_service
from . import provisioning_deletion
from .provisioning_dispatch_binding import DispatchBinding
from .provisioning_host import HostIdentity

SECRET_FIELDS = ("mt_username", "mt_password", "se_admin_password", "xr_ssh_username", "xr_ssh_password",
    "xr_ssh_private_key", "xr_panel_username", "xr_panel_password", "xr_panel_api_token")
IDENTITY_FIELDS = ("wg_interface", "wg_peer_name", "wg_public_key", "wg_client_address", "wg_gateway_with_prefix",
    "xr_inbound_tag", "xr_panel_inbound_id", "xr_email", "account_username", "flow")


def snapshot(db, step_id, version, identity, leases, *, recovery_read):
    return _snapshot(db, step_id, version, identity, leases, recovery_read=recovery_read, phase="forward")


def compensation_snapshot(db, step_id, version, identity, leases):
    """Stored cleanup descriptor; no dispatch, refund, release or commit.

    Expired forward deadlines and disabled nodes cannot strand a known
    compensation identity. Ownership, leases, endpoint and contract remain
    mandatory. This private helper is not a real absence action registration.
    """
    return _snapshot(db, step_id, version, identity, leases, recovery_read=False, phase="compensation")


def deletion_snapshot(db, step_id, version, identity, leases):
    """Stored removal descriptor; no dispatch, finalization or commit.

    A staged/cleanup-required step is marked remote_calling in this same
    fenced transaction. A remote_calling retry only re-reads the committed
    identity. ensure_absent is idempotent, so deletion never borrows the
    create-compensation state machine.
    """
    return _snapshot(db, step_id, version, identity, leases, recovery_read=False, phase="deletion")


def _snapshot(db, step_id, version, identity, leases, *, recovery_read, phase):
    forward, deleting = phase == "forward", phase == "deletion"
    if type(phase) is not str or phase not in ("forward", "compensation", "deletion") or (
            not isinstance(identity, HostIdentity)) or type(recovery_read) is not bool or type(step_id) is not int or (
            step_id < 1 or type(version) is not int or version < 0):
        raise HTTPException(422, "provisioning_dispatch_invalid")
    if db.get_transaction() is None or db.info.get("resource_lease_business_transaction") is not db.get_transaction():
        raise HTTPException(409, "provisioning_dispatch_transaction_not_fenced")
    provisioning_schema.assert_ready()
    # L1 control rows precede L2 resource leases and L4 operation/step rows.
    wallet_service.require_legacy_phase(db)
    wallet_epoch = db.scalar(select(rv.wallet_runtime_state.c.epoch).where(rv.wallet_runtime_state.c.id == 1)
        .with_for_update(read=True))
    kind = db.scalar(select(mp.ProvisioningOperation.operation_type).join(mp.ProvisioningStep,
        mp.ProvisioningStep.operation_id == mp.ProvisioningOperation.id).where(mp.ProvisioningStep.id == step_id))
    if kind is None:
        raise HTTPException(404, "provisioning_step_not_found")
    if db.scalar(select(mp.ProvisioningTypeMode.mode).where(
            mp.ProvisioningTypeMode.operation_type == kind).with_for_update(read=True)) != "durable":
        raise HTTPException(503, "provisioning_type_not_durable")
    runtime = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if runtime is None or runtime.owner_state not in ("active", "draining") or runtime.gate_mode != "enforced" or (
            runtime.owner_host_id, runtime.owner_boot_id) != (identity.host_id, identity.boot_id):
        raise HTTPException(409, "provisioning_dispatch_owner_changed")
    if runtime.owner_state == "draining":
        raise HTTPException(503, "provisioning_draining")
    tokens = tuple(leases)
    if not tokens:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    operation, step = transitions._step(db, step_id, version)
    expected_state = (("remote_calling" if recovery_read else "staged") if forward else
                      ("staged", "cleanup_required", "remote_calling") if deleting else "compensating")
    valid_kinds = (("delete_user", "delete_purchase", "delete_connection") if deleting else
                   ("create_user", "purchase", "add_connection"))
    valid_operation_states = (("prepared", "provisioning", "cleanup_required") if deleting else
                              ("prepared", "provisioning") if forward else ("compensating",))
    if operation.operation_type != kind or kind not in valid_kinds or step.direction != (
            "remove" if deleting else "create") or operation.state not in valid_operation_states or (
            step.state not in expected_state if deleting else step.state != expected_state) or (
            not forward and not deleting and not step.remote_attempted):
        raise HTTPException(409, "provisioning_dispatch_state_invalid")
    owner = {token.owner for token in tokens}
    operation_token = next((token for token in tokens if token.resource_key == f"provisioning_op:{operation.id}"), None)
    if len(owner) != 1 or operation_token is None:
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    panel = db.get(models.PanelSettings, 1, populate_existing=True)
    if panel is not None and panel.ha_enabled:
        raise HTTPException(503, "provisioning_ha_unsupported")
    clock, _ = resource_leases._clock(db)
    now = db.scalar(select(clock))
    if isinstance(now, str):
        now = dt.datetime.fromisoformat(now)
    if forward and (operation.forward_deadline <= now or wallet_epoch != operation.wallet_epoch_at_start):
        raise HTTPException(409, "provisioning_dispatch_window_changed")
    if any(retry is not None and retry > now for retry in (operation.next_retry_at, step.next_retry_at)):
        # Recheck after the operation/step locks: a direct dispatch or a
        # stale worker selection cannot bypass committed backoff or mark
        # an attempt before it is due. No management secrets loaded yet.
        raise HTTPException(409, "provisioning_retry_not_due")
    node = db.get(models.Node, step.node_id, populate_existing=True)
    if node is None or (forward and not node.enabled) or contracts.backend_for(node, step.protocol) != step.backend:
        raise HTTPException(409, "node_unavailable")
    if node.xr_panel_base_url:
        try:
            url = node.xr_panel_base_url
            parsed = urlsplit(url if url.startswith(("http://", "https://")) else "http://" + url)
            if parsed.username or parsed.password:
                raise ValueError()
        except ValueError:
            raise HTTPException(422, "provisioning_dispatch_public_credentials_invalid") from None
    contract = contracts.require_ready(db, node, step.backend)
    try:
        saved = json.loads(operation.intent)
        if deleting:
            connection = db.get(models.Connection, step.connection_id, populate_existing=True)
            if connection is None or saved["connection_fingerprints"][str(connection.id)] != (
                    provisioning_deletion._fingerprint(connection, node)):
                raise ValueError()
        elif saved["node_fingerprints"][str(node.id)] != contracts.config_fingerprint(node):
            raise ValueError()
    except (TypeError, ValueError, KeyError):
        raise HTTPException(409, "provisioning_dispatch_endpoint_changed") from None
    if step.backend == "radius_ppp":
        raise HTTPException(409, "provisioning_dispatch_not_remote")
    if forward and not recovery_read:
        other_calling = db.scalar(select(mp.ProvisioningStep.id).where(
            mp.ProvisioningStep.operation_id == operation.id, mp.ProvisioningStep.id != step.id,
            mp.ProvisioningStep.state == "remote_calling").limit(1))
        if other_calling is not None:
            raise HTTPException(409, "provisioning_remote_recovery_pending")
        step = transitions.begin_remote(db, step.id, step.version)
    elif deleting and step.state != "remote_calling":
        step = transitions.begin_remove(db, step.id, step.version)
        if step.state == "removed":
            raise HTTPException(409, "provisioning_dispatch_not_remote")
    # Materialize refreshed CAS versions before serialization; the helper
    # never commits, and a failed caller transaction rolls this mark back.
    binding = DispatchBinding(runtime.installation_uuid, runtime.ownership_epoch, runtime.gate_mode_epoch,
        operation.id, operation.version, step.id, step.version, node.id, step.backend,
        operation_token.owner, operation_token.epoch, phase)
    public = {field: getattr(node, field) for field in rules.ENDPOINT_FIELDS}
    public["type"] = node.type.value
    action = (remote_action.ActionType.WG_ENSURE_PRESENT if step.backend == "mikrotik_wg" else
              remote_action.ActionType.SOFTETHER_ENSURE_PRESENT if step.backend == "softether" else
              remote_action.ActionType.XRAY_ENSURE_PRESENT)
    if not forward:
        action = (remote_action.ActionType.WG_ENSURE_ABSENT if step.backend == "mikrotik_wg" else
                  remote_action.ActionType.SOFTETHER_ENSURE_ABSENT if step.backend == "softether" else
                  remote_action.ActionType.XRAY_ENSURE_ABSENT)
    return remote_action.RemoteActionDTO(action_type=action, node_id=node.id, backend=step.backend,
        node_config=public, node_secrets={field: getattr(node, field) for field in SECRET_FIELDS},
        identity={field: getattr(step, field) for field in IDENTITY_FIELDS},
        # RouterOS needs the PUBLIC key only. The customer's private key stays
        # staged in the parent DB for atomic final delivery; never send it to
        # a child process / node, even for read recovery.
        credential={"wg_private_key": None, "password": None if deleting else step.staged_password,
                    "uuid": step.staged_xr_uuid}, params={"recovery_read": recovery_read,
                    "max_concurrent_sessions": step.max_concurrent_sessions, "speed_limit_mbps": step.speed_limit_mbps},
        timeouts={"connect_timeout": 10, "read_timeout": 15,
                  "hard_deadline": min(120, (operation.forward_deadline - now).total_seconds()) if forward else 120},
        contract=contract, fencing={"installation_uuid": runtime.installation_uuid,
                                   "binding": asdict(binding), "host_id": identity.host_id,
                                   "boot_id": identity.boot_id, "expected_parent_pid": os.getpid()})
