"""Runner-child execution of one LEGACY writer block (Lifecycle 10.7, 10.8).

A legacy writer has no provisioning step and its node needs no ready
contract: it keeps today's semantics (legacy_remote_blocks). What it must
have in 'enforced' is the same serialization and fencing as any remote
write, re-checked from scratch before EVERY write by LegacyChildGuard:

  * this process is the runner child (parent-death signal armed) and holds
    the shared gate-mode lock and the node's exclusive lock of THIS
    installation, plus on MariaDB the node's GET_LOCK on its own connection;
  * the runtime row: same installation, gate 'enforced' with the epoch the
    parent saw, owner 'active' = this host and boot, same ownership epoch;
  * the installation file names the same installation;
  * the node still exists with exactly the endpoint the parent pinned.

Writes go only through the guarded child transports, which ask
provisioning_child_authority.require_write -> this guard's check().
"""
from __future__ import annotations

import os
import re
import uuid
from dataclasses import asdict, dataclass

from sqlalchemy import text

from . import gate_locks, provisioning_child_authority as authority, provisioning_contract_rules as rules
from .provisioning_child_database import (ADVISORY_CHECK, ADVISORY_GET, ADVISORY_RELEASE, READ_RUNTIME, READ_SUBNET,
                                          ChildDatabase)
from .remote_action import Outcome, RemoteActionResult

BACKENDS = ("mikrotik_wg", "softether", "xray_ssh", "threexui", "marzban", "hiddify", "marzneshin", "sui")


class LegacyGuardUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyBinding:
    installation_uuid: str
    ownership_epoch: int
    gate_mode_epoch: int
    node_id: int
    backend: str
    endpoint_fingerprint: str
    phase: str = "legacy"

    def __post_init__(self):
        try:
            canonical = str(uuid.UUID(self.installation_uuid)) == self.installation_uuid
        except (TypeError, ValueError, AttributeError):
            canonical = False
        if not canonical or self.phase not in ("legacy", "probe") or self.backend not in BACKENDS or any(
                type(value) is not int or not 0 <= value < 2 ** 63 for value in (self.ownership_epoch, self.gate_mode_epoch)) or (
                type(self.node_id) is not int or self.node_id <= 0 or self.ownership_epoch <= 0) or (
                not isinstance(self.endpoint_fingerprint, str) or not re.fullmatch("[0-9a-f]{64}", self.endpoint_fingerprint)):
            raise LegacyGuardUnavailable("legacy_binding_invalid")


class LegacyChildGuard:
    def __init__(self, database, binding, identity, mode_hold, node_hold, *, base_dir=None):
        if not isinstance(database, ChildDatabase) or not isinstance(binding, LegacyBinding):
            raise LegacyGuardUnavailable("legacy_guard_invalid")
        self.database, self.binding, self.identity = database, binding, identity
        self.mode_hold, self.node_hold, self.base_dir = mode_hold, node_hold, base_dir
        self._connection = None
        self._entered = self._broken = False

    # Not a durable deletion guard (provisioning_child_authority asks).
    removal_sealed = False

    def _holds(self):
        uuid_ = self.binding.installation_uuid
        if not (isinstance(self.mode_hold, gate_locks.FileLock) and self.mode_hold.held and self.mode_hold.shared is True
                and self.mode_hold.path == gate_locks.mode_lock_path(uuid_, self.base_dir)):
            raise LegacyGuardUnavailable("legacy_guard_mode_lock_missing")
        if not (isinstance(self.node_hold, gate_locks.FileLock) and self.node_hold.held and self.node_hold.shared is False
                and self.node_hold.path == gate_locks.node_lock_path(uuid_, self.binding.node_id, self.base_dir)):
            raise LegacyGuardUnavailable("legacy_guard_node_lock_missing")

    def __enter__(self):
        if self._entered or self._broken:
            raise LegacyGuardUnavailable("legacy_guard_not_reusable")
        try:
            self._holds()
            if self.database.dialect.name in ("mysql", "mariadb"):
                self._connection = self.database.connect()
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                if self._connection.execute(text(ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() != 1:
                    raise LegacyGuardUnavailable("legacy_guard_advisory_unavailable")
                self._connection.end_snapshot()
            self._entered = True
            self.check()
            return self
        except Exception:
            self._broken = True
            self._close()
            raise LegacyGuardUnavailable("legacy_guard_unavailable") from None

    def check(self):
        if not self._entered or self._broken:
            raise LegacyGuardUnavailable("legacy_guard_unavailable")
        try:
            self._holds()
            if self._connection is not None:
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                if self._connection.execute(text(ADVISORY_CHECK), dict(name=name)).scalar_one() != 1:
                    raise LegacyGuardUnavailable("legacy_guard_advisory_lost")
                self._connection.end_snapshot()
            from .provisioning_installation import read_file
            if read_file(self.base_dir) != self.binding.installation_uuid:
                raise LegacyGuardUnavailable("legacy_guard_installation_mismatch")
            expected_backend = "flock" if self.database.dialect.name == "sqlite" else "flock+get_lock"
            with self.database.connect() as connection:
                runtime = connection.execute(text(READ_RUNTIME)).mappings().one_or_none()
                node = connection.execute(rules.node_endpoint_statement(),
                                          dict(node_id=self.binding.node_id)).mappings().one_or_none()
            if runtime is None or runtime["installation_uuid"] != self.binding.installation_uuid or (
                    runtime["gate_mode"] != "enforced" or runtime["gate_mode_epoch"] != self.binding.gate_mode_epoch) or (
                    runtime["owner_state"] != "active" or runtime["ownership_epoch"] != self.binding.ownership_epoch) or (
                    (runtime["owner_host_id"], runtime["owner_boot_id"]) != (self.identity.host_id, self.identity.boot_id)) or (
                    runtime["lock_backend"] != expected_backend):
                raise LegacyGuardUnavailable("legacy_guard_ownership_changed")
            if node is None or rules.endpoint_fingerprint(node) != self.binding.endpoint_fingerprint:
                raise LegacyGuardUnavailable("legacy_guard_node_changed")
            return self.binding
        except Exception:
            self._broken = True
            self._close()
            raise LegacyGuardUnavailable("legacy_guard_unavailable") from None

    def _close(self):
        if self._connection is not None:
            connection, self._connection = self._connection, None
            try:
                name = gate_locks.get_lock_name(self.binding.installation_uuid, self.binding.node_id)
                connection.execute(text(ADVISORY_RELEASE), dict(name=name))
            except Exception:
                pass
            finally:
                try:
                    connection.close()
                except Exception:
                    pass

    def __exit__(self, *exception):
        self._broken = True
        self._close()


def _client(guard, kind, config, secrets):
    """A guarded child client for the pinned endpoint (never a legacy one)."""
    from .provisioning_child_routeros import ChildMikrotikClient
    from .provisioning_child_softether import ChildSoftEtherClient
    from .provisioning_child_ssh import ChildXrayClient
    from .provisioning_child_http import ChildHttpSession
    binding = guard.check()
    public = {field: config.get(field) for field in rules.ENDPOINT_FIELDS}
    if rules.endpoint_fingerprint({"n_" + k: v for k, v in public.items()}) != binding.endpoint_fingerprint:
        raise LegacyGuardUnavailable("legacy_client_endpoint_changed")
    secret = lambda key: secrets.get(key) or ""
    node_id, backend = binding.node_id, binding.backend
    if kind == "mikrotik" and backend == "mikrotik_wg":
        port = public["mt_api_ssl_port"] if public["mt_use_ssl"] else public["mt_port"]
        return ChildMikrotikClient(public["mt_host"], secret("mt_username"), secret("mt_password"), port,
                                   public["mt_use_ssl"], node_id=node_id)
    if kind == "softether" and backend == "softether":
        return ChildSoftEtherClient(public["se_host"], public["se_port"], public["se_hub_name"],
                                    secret("se_admin_password"), node_id=node_id,
                                    verify_tls=bool(config.get("se_verify_tls")))
    if kind != "xray":
        raise LegacyGuardUnavailable("legacy_client_kind_mismatch")
    if backend == "xray_ssh":
        return ChildXrayClient(public["xr_ssh_host"], secret("xr_ssh_username"), public["xr_ssh_port"],
                               secret("xr_ssh_password"), secret("xr_ssh_private_key"), public["xr_config_path"],
                               public["xr_service_name"], config.get("xr_api_address") or "127.0.0.1:10085",
                               node_id=node_id)
    from .provisioning_child_clients import _Sui
    from .threexui_client import ThreeXUIClient
    from .marzban_client import MarzbanClient
    from .marzneshin_client import MarzneshinClient
    from .hiddify_client import HiddifyClient
    url, inbound = public["xr_panel_base_url"], public["xr_panel_inbound_id"]
    if backend == "sui":
        return _Sui(url, secret("xr_panel_api_token"), inbound, node_id)
    if backend == "threexui":
        client = ThreeXUIClient(url, secret("xr_panel_username"), secret("xr_panel_password"), inbound,
                                api_token=secret("xr_panel_api_token"))
    elif backend == "marzban":
        client = MarzbanClient(url, secret("xr_panel_username"), secret("xr_panel_password"), public["xr_inbound_tag"])
    elif backend == "marzneshin":
        client = MarzneshinClient(url, secret("xr_panel_username"), secret("xr_panel_password"), inbound)
    elif backend == "hiddify":
        client = HiddifyClient(url, secret("xr_panel_api_token"))
    else:
        raise LegacyGuardUnavailable("legacy_client_backend_unsupported")
    session = ChildHttpSession(client.base_url, node_id, backend)
    session.headers.update(client.session.headers)
    client.session.close()
    client.session = session
    return client


_URL = re.compile(r"[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def sanitize(message, secrets) -> str:
    """A client error for the caller: no URL (panel paths can embed tokens), no secret value."""
    text_ = _URL.sub("<url>", str(message or ""))
    for value in secrets:
        if isinstance(value, str) and len(value) >= 4:
            text_ = text_.replace(value, "***")
    return text_[:300]


def run(dto):
    """Registered runner entry for every legacy block."""
    from . import legacy_remote_blocks as blocks
    from .provisioning_child_entry import _holds as held
    from .provisioning_host import read_identity
    authority._role()
    holds = held.get()
    if holds is None or holds[:2] != (os.getpid(), __import__("threading").get_ident()):
        raise LegacyGuardUnavailable("legacy_entry_locks_missing")
    block = dto.params.get("block")
    if block not in blocks.BLOCKS or not isinstance(dto.params.get("args"), dict):
        raise LegacyGuardUnavailable("legacy_block_invalid")
    kind = blocks.CLIENT_KIND[block]
    trusted_url = os.environ.get("DATABASE_URL", "").strip()
    if not trusted_url:
        raise LegacyGuardUnavailable("legacy_entry_configuration_missing")
    binding = LegacyBinding(**dto.fencing["binding"])
    if binding.phase != "legacy" or (binding.node_id, binding.backend) != (dto.node_id, dto.backend):
        raise LegacyGuardUnavailable("legacy_binding_mismatch")
    database = ChildDatabase(trusted_url)
    secrets = list(dto.node_secrets.values()) + [v for v in dto.credential.values() if isinstance(v, str)]
    try:
        with LegacyChildGuard(database, binding, read_identity(), holds[2], holds[3]) as guard:
            with authority._scope(guard):
                client = _client(guard, kind, dto.node_config, dto.node_secrets)
                arguments = dict(dto.params["args"], **dto.credential)
                if block == "wg_reconcile":
                    # 9.1: the subnet is read now, under the node lock - never the dispatch-time copy.
                    with database.connect() as connection:
                        fresh = connection.execute(text(READ_SUBNET), dict(node_id=binding.node_id)).scalar_one_or_none()
                    if not fresh:
                        raise LegacyGuardUnavailable("legacy_reconcile_subnet_missing")
                    arguments["desired_subnet"] = fresh
                try:
                    with client:
                        value = blocks.BLOCKS[block](client, **arguments)
                except LegacyGuardUnavailable:
                    raise
                except authority.WriteAuthorityUnavailable:
                    raise
                except Exception as exc:  # noqa: BLE001 - a client error, reported like the site would raise it
                    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.TRANSPORT_ERROR,
                                              write_attempted=True, error_code="client_error",
                                              error_sanitized=sanitize(exc, secrets),
                                              observed={"error_class": type(exc).__name__})
        return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED, write_attempted=True,
                                  observed={"value": value})
    finally:
        database.dispose()


def binding_dict(binding: LegacyBinding) -> dict:
    return asdict(binding)
