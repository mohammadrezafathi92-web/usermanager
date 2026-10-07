"""Read-only committed-step binding for a future durable child runner.

Invoke AFTER taking mode SH and node EX; release the DB connection BEFORE
remote I/O, retain the file locks throughout the action. This is NOT a
transport authorization token: action schema, current node contract, adapter
version and read-only child connection setup still need separate validation.
No live caller, runner registration, client, ORM import or mode activation.
Only unapproved create/purchase/add-connection operations are supported.
"""
import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from . import gate_locks, provisioning_installation
from .provisioning_host import HostIdentity

ENDPOINT_FIELDS = ("type", "xr_panel_mode", "mt_host", "mt_port", "mt_use_ssl", "mt_api_ssl_port",
    "mt_wireguard_interface", "xr_ssh_host", "xr_ssh_port", "xr_panel_base_url", "se_host", "se_port",
    "se_hub_name", "xr_inbound_tag", "xr_panel_inbound_id", "xr_config_path", "xr_service_name")
XRAY_MODES = {"xray_ssh": "ssh", "threexui": "3xui", "marzban": "marzban", "hiddify": "hiddify",
    "marzneshin": "marzneshin", "sui": "sui"}


class DispatchBindingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class DispatchBinding:
    installation_uuid: str
    ownership_epoch: int
    gate_mode_epoch: int
    operation_id: int
    operation_version: int
    step_id: int
    step_version: int
    node_id: int
    backend: str
    lease_owner: str
    lease_epoch: int
    phase: str = "forward"

    def __post_init__(self):
        positive = (self.ownership_epoch, self.operation_id, self.step_id, self.node_id, self.lease_epoch)
        versions = (self.gate_mode_epoch, self.operation_version, self.step_version)
        if any(type(value) is not int or not 0 < value < 2 ** 63 for value in positive) or any(
                type(value) is not int or not 0 <= value < 2 ** 63 for value in versions) or (
                type(self.phase) is not str or type(self.backend) is not str or
                self.phase not in ("forward", "compensation") or
                self.backend not in ("mikrotik_wg", "softether", *XRAY_MODES)):
            raise DispatchBindingUnavailable("durable_dispatch_invalid")
        try:
            valid = str(uuid.UUID(self.installation_uuid)) == self.installation_uuid
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid or not isinstance(self.lease_owner, str) or len(self.lease_owner) > 64 or not re.fullmatch(
                f"op:{self.operation_id}:[1-9][0-9]*", self.lease_owner):
            raise DispatchBindingUnavailable("durable_dispatch_invalid")


def _endpoint(row):
    values = {name: row["n_" + name] for name in ENDPOINT_FIELDS}
    if values["mt_use_ssl"] is not None:
        values["mt_use_ssl"] = bool(values["mt_use_ssl"])
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dispatch_statement(dialect):
    """The exact closed read used by the child's SQL allowlist."""
    if dialect == "sqlite":
        clock = "strftime('%Y-%m-%d %H:%M:%f','now')"
    elif dialect in ("mysql", "mariadb"):
        clock = "NOW(6)"
    else:
        raise DispatchBindingUnavailable("durable_dispatch_database_unsupported")
    endpoints = ", ".join("n." + name + " AS n_" + name for name in ENDPOINT_FIELDS)
    # One fresh committed snapshot. Epoch/deadline is checked by the DB clock,
    # not the DTO's timestamp. Only constant identifiers enter the SQL text.
    return text(f"""SELECT r.installation_uuid, r.owner_state, r.owner_host_id, r.owner_boot_id,
      r.ownership_epoch, r.gate_mode, r.gate_mode_epoch, r.lock_backend,
      o.operation_type, o.state AS operation_state, o.version AS operation_version, o.intent,
      o.approval_uuid, o.target_user_id, o.username_claim, o.wallet_epoch_at_start,
      CASE WHEN o.forward_deadline > {clock} THEN 1 ELSE 0 END AS forward_live,
      s.state AS step_state, s.version AS step_version, s.backend, s.direction, s.protocol, s.remote_attempted,
      s.node_id, s.wg_interface, s.xr_inbound_tag, s.xr_panel_inbound_id,
      l.lease_owner, l.fencing_epoch, CASE WHEN l.leased_until >= {clock} THEN 1 ELSE 0 END AS lease_live,
      m.mode AS type_mode, w.epoch AS wallet_epoch, COALESCE(p.ha_enabled,0) AS ha_enabled,
      u.id AS user_id, u.username AS user_username, u.owner_admin_id AS user_owner, u.telegram_id AS user_telegram,
      n.enabled AS node_enabled, {endpoints}
      FROM provisioning_runtime_state r
      JOIN provisioning_operations o ON o.id=:operation_id
      JOIN provisioning_steps s ON s.id=:step_id AND s.operation_id=o.id
      JOIN nodes n ON n.id=s.node_id
      JOIN provisioning_type_modes m ON m.operation_type=o.operation_type
      JOIN wallet_runtime_state w ON w.id=1
      JOIN resource_locks l ON l.resource_key=:lease_key
      LEFT JOIN users u ON u.id=o.target_user_id OR (o.target_user_id IS NULL AND u.username=o.username_claim)
      LEFT JOIN panel_settings p ON p.id=1
      WHERE r.id=1""")


def revalidate(engine, binding, identity, mode_hold, node_hold, *, base_dir=None):
    if not isinstance(binding, DispatchBinding) or not isinstance(identity, HostIdentity):
        raise DispatchBindingUnavailable("durable_dispatch_invalid")
    expected = ((mode_hold, True, gate_locks.mode_lock_path(binding.installation_uuid, base_dir)),
        (node_hold, False, gate_locks.node_lock_path(binding.installation_uuid, binding.node_id, base_dir)))
    if any(not isinstance(hold, gate_locks.FileLock) or not hold.held or hold.shared is not shared or hold.path != path
           for hold, shared, path in expected):
        raise DispatchBindingUnavailable("durable_dispatch_locks_missing")
    try:
        if provisioning_installation.read_file(base_dir) != binding.installation_uuid:
            raise DispatchBindingUnavailable("durable_dispatch_namespace_mismatch")
    except provisioning_installation.InstallationUnavailable:
        raise DispatchBindingUnavailable("durable_dispatch_namespace_unavailable") from None
    statement = dispatch_statement(engine.dialect.name)
    try:
        with engine.connect() as connection:
            row = connection.execute(statement, dict(operation_id=binding.operation_id, step_id=binding.step_id,
                lease_key=f"provisioning_op:{binding.operation_id}")).mappings().one_or_none()
    except SQLAlchemyError:
        raise DispatchBindingUnavailable("durable_dispatch_database_unavailable") from None
    forward = binding.phase == "forward"
    if row is None or any((
        row["installation_uuid"] != binding.installation_uuid,
        row["owner_state"] not in ("active", "draining"),
        (row["owner_host_id"], row["owner_boot_id"]) != (identity.host_id, identity.boot_id),
        row["ownership_epoch"] != binding.ownership_epoch, row["gate_mode"] != "enforced",
        row["gate_mode_epoch"] != binding.gate_mode_epoch,
        row["lock_backend"] != ("flock" if engine.dialect.name == "sqlite" else "flock+get_lock"),
        row["operation_type"] not in ("create_user", "purchase", "add_connection"), row["approval_uuid"] is not None,
        row["operation_version"] != binding.operation_version, row["step_version"] != binding.step_version,
        row["operation_state"] not in (("provisioning",) if forward else ("compensating", "cleanup_required")),
        row["step_state"] != ("remote_calling" if forward else "compensating"), row["direction"] != "create",
        not row["remote_attempted"], row["node_id"] != binding.node_id, row["backend"] != binding.backend,
        row["type_mode"] != "durable", bool(row["ha_enabled"]),
        (row["lease_owner"], row["fencing_epoch"], bool(row["lease_live"])) != (
            binding.lease_owner, binding.lease_epoch, True),
        forward and (not row["node_enabled"] or not row["forward_live"] or
                     row["wallet_epoch"] != row["wallet_epoch_at_start"]),
    )):
        raise DispatchBindingUnavailable("durable_dispatch_stale")
    try:
        saved = json.loads(row["intent"])
        if type(saved.get("schema_version")) is not int or saved["schema_version"] != 1 or (
                saved["node_fingerprints"][str(binding.node_id)] != _endpoint(row)):
            raise ValueError()
        if forward:
            user = saved["user"]
            if row["operation_type"] == "create_user":
                if row["target_user_id"] is not None or row["user_id"] is not None or row["username_claim"] != user["username"]:
                    raise ValueError()
            elif row["user_id"] is None or (row["user_username"], row["user_owner"], row["user_telegram"]) != (
                    user["username"], user["owner_admin_id"], user["telegram_id"]):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise DispatchBindingUnavailable("durable_dispatch_stale") from None
    if binding.backend == "mikrotik_wg":
        valid = row["n_type"] == "mikrotik" and row["protocol"] == "wireguard" and (
            row["wg_interface"] == row["n_mt_wireguard_interface"])
    elif binding.backend == "softether":
        valid = row["n_type"] == "softether" and row["protocol"] == "softether"
    else:
        valid = row["n_type"] == "xray" and row["protocol"] == "xray" and (
            (row["n_xr_panel_mode"] or "ssh") == XRAY_MODES[binding.backend]) and (
            row["xr_inbound_tag"], row["xr_panel_inbound_id"]) == (
            row["n_xr_inbound_tag"], row["n_xr_panel_inbound_id"])
    if not valid:
        raise DispatchBindingUnavailable("durable_dispatch_stale")
    return binding  # Committed-step binding only, never a transport token.
