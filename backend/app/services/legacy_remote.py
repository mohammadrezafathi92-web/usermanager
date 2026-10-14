"""Run one legacy remote block in the right place (Lifecycle 10.6, 10.8).

    legacy_remote.run(node, "wg_set_enabled", interface=..., peer_name=..., enabled=False)

  gate off / shadow  inline, inside writer_gate, with the legacy client:
                     exactly what the site did before (same exceptions).
  gate enforced      the parent holds the shared gate-mode lock, checks that
                     THIS host owns the installation, and sends the block to
                     the runner child, which takes the node lock itself,
                     re-checks ownership, installation and the node's
                     endpoint, and runs the same block with a guarded client.
                     A client error comes back as the same exception class
                     with a sanitized message; an unknown outcome (killed,
                     transport) is raised as an error, never as success.

The node's management credentials and the customer's credential travel only
in the DTO's two secret fields, over the runner's private pipe.
"""
from __future__ import annotations

import os
from typing import Any

from . import legacy_remote_blocks as blocks
from . import node_gate, provisioning_contract_rules as rules
from .mikrotik_client import MikrotikClient, MikrotikError
from .remote_action import ActionType, Outcome, RemoteActionDTO
from .softether_client import SoftEtherError, client_for_node as softether_client_for_node
from .transport_guard import GateViolation
from .xray_client import XrayError, client_for_node as xray_client_for_node

HARD_DEADLINE_SECONDS = 120          # Lifecycle 18 decision 14 (default)

ERRORS = {"mikrotik": MikrotikError, "xray": XrayError, "softether": SoftEtherError}
SECRETS = {
    "mikrotik": ("mt_username", "mt_password"),
    "softether": ("se_admin_password",),
    "xray": ("xr_ssh_username", "xr_ssh_password", "xr_ssh_private_key", "xr_panel_username",
             "xr_panel_password", "xr_panel_api_token"),
}
# Non-secret node values a client needs that are not part of the endpoint fingerprint.
EXTRA_PUBLIC = {"mikrotik": (), "softether": ("se_verify_tls",), "xray": ("xr_api_address",)}

ACTION_FOR_BLOCK = {
    "wg_provision": ActionType.WG_ENSURE_PRESENT, "xray_add": ActionType.XRAY_ENSURE_PRESENT,
    "softether_add": ActionType.SOFTETHER_ENSURE_PRESENT, "wg_remove": ActionType.WG_ENSURE_ABSENT,
    "xray_remove": ActionType.XRAY_ENSURE_ABSENT, "softether_remove": ActionType.SOFTETHER_ENSURE_ABSENT,
    "ppp_kick": ActionType.PPP_KICK_SESSION, "wg_bounce": ActionType.WG_BOUNCE_PEER,
    "xray_bounce": ActionType.XRAY_BOUNCE_CLIENT, "softether_bounce": ActionType.SOFTETHER_BOUNCE_USER,
    "wg_set_enabled": ActionType.WG_SET_PEER_ENABLED, "xray_set_enabled": ActionType.XRAY_SET_CLIENT_ENABLED,
    "softether_set_enabled": ActionType.SOFTETHER_SET_USER_ENABLED, "wg_rename": ActionType.WG_RENAME_PEER,
    "wg_queue_sync": ActionType.WG_SET_SPEED_QUEUE, "xray_rebuild": ActionType.XRAY_REBUILD_CLIENTS,
    "wg_reconcile": ActionType.WG_RECONCILE_INTERFACE_ADDRESS,
}
PUSH_ACTIONS = {"push_radius_config": ActionType.MT_PUSH_RADIUS, "push_pptp_config": ActionType.MT_PUSH_PPTP,
                "push_sstp_config": ActionType.MT_PUSH_SSTP, "push_l2tp_config": ActionType.MT_PUSH_L2TP,
                "push_ikev2_config": ActionType.MT_PUSH_IKEV2}


def _legacy_client(node, kind):
    if kind == "mikrotik":
        return MikrotikClient.for_node(node)
    if kind == "softether":
        return softether_client_for_node(node)
    return xray_client_for_node(node)


def backend_of(node) -> str:
    from .. import models
    from .provisioning_identity import XRAY_MODES
    if node.type == models.NodeType.mikrotik:
        return "mikrotik_wg"
    if node.type == models.NodeType.softether:
        return "softether"
    mode = getattr(node, "xr_panel_mode", None) or "ssh"
    return next(backend for backend, value in XRAY_MODES.items() if value == mode)


def endpoint(node) -> dict:
    values = {field: getattr(node, field, None) for field in rules.ENDPOINT_FIELDS}
    values["type"] = node.type.value
    return values


def run(node, block: str, *, factory=None, action_type: ActionType | None = None, **arguments) -> Any:
    """`factory(node)` builds the legacy client for the inline path; each site
    passes its own module-level factory (the seam its tests replace)."""
    if block not in blocks.BLOCKS:
        raise ValueError(f"unknown legacy block {block!r}")
    kind = blocks.CLIENT_KIND[block]
    with node_gate.writer_passage(node) as passage:
        if not isinstance(passage, node_gate.EnforcedPassage):
            with (factory(node) if factory is not None else _legacy_client(node, kind)) as client:
                return blocks.BLOCKS[block](client, **arguments)
        return _dispatch(node, passage, block, kind, action_type or ACTION_FOR_BLOCK.get(block), arguments)


def _owner_snapshot(passage):
    """This host must own the installation now; the child checks again."""
    from .. import models_provisioning as mp
    from ..database import SessionLocal
    from .provisioning_host import HostIdentityUnavailable, read_identity
    try:
        identity = read_identity()
    except HostIdentityUnavailable:
        raise GateViolation("host identity unavailable in 'enforced'") from None
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1, populate_existing=True)
        if row is None or row.gate_mode != "enforced" or row.owner_state != "active" or (
                row.installation_uuid, row.gate_mode_epoch) != (passage.installation_uuid, passage.gate_mode_epoch) or (
                row.owner_host_id, row.owner_boot_id) != (identity.host_id, identity.boot_id):
            raise GateViolation("this host does not own the enforced gate")
        return dict(installation_uuid=row.installation_uuid, ownership_epoch=int(row.ownership_epoch),
                    gate_mode_epoch=int(row.gate_mode_epoch))


def _dispatch(node, passage, block, kind, action_type, arguments):
    if action_type is None:
        raise GateViolation(f"legacy block {block!r} has no runner action")
    owner = _owner_snapshot(passage)
    public = endpoint(node)
    binding = dict(installation_uuid=owner["installation_uuid"], ownership_epoch=owner["ownership_epoch"],
                   gate_mode_epoch=owner["gate_mode_epoch"], node_id=node.id, backend=backend_of(node),
                   endpoint_fingerprint=rules.endpoint_fingerprint({"n_" + k: v for k, v in public.items()}),
                   phase="legacy")
    config = dict(public, **{name: getattr(node, name, None) for name in EXTRA_PUBLIC[kind]})
    secrets = {name: getattr(node, name, None) for name in SECRETS[kind]}
    credential = {name: value for name, value in arguments.items() if name in blocks.CREDENTIAL_ARGUMENTS}
    params = {"block": block, "args": {name: value for name, value in arguments.items()
                                       if name not in blocks.CREDENTIAL_ARGUMENTS}}
    dto = RemoteActionDTO(action_type=action_type, node_id=node.id, backend=binding["backend"], node_config=config,
                          node_secrets=secrets, credential=credential, params=params,
                          timeouts={"hard_deadline": HARD_DEADLINE_SECONDS},
                          fencing={"installation_uuid": owner["installation_uuid"], "binding": binding,
                                   "expected_parent_pid": os.getpid()})
    from . import remote_runner
    result = remote_runner.run_action(dto)
    error = ERRORS[kind]
    if result.outcome == Outcome.SUCCEEDED:
        return result.observed.get("value")
    if result.error_code == "client_error":
        raise error(result.error_sanitized or "عملیات روی نود ناموفق بود")
    if result.is_unknown:
        raise error("نتیجه‌ی عملیات روی نود نامعلوم است (runner)؛ دوباره بررسی کنید")
    raise error(f"عملیات روی نود انجام نشد ({result.error_code or result.outcome.value})")
