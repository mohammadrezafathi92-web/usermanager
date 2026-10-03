"""Versioned, serializable contract between the panel process and the
killable mutation runner (Lifecycle/P6 design v7.1, section 10.9).

Nothing in this module talks to a database, a node or a process. It only
defines WHAT crosses the process boundary:

- RemoteActionDTO    parent -> child: one remote action, fully described
- RemoteActionResult child -> parent: what happened, as one of eight outcomes

Both are plain data. A Session, a Query, an ORM object, a client, a
callback or a closure can not be put into either: every value is checked
recursively against a closed set of JSON types at construction time, and
the wire format is JSON (never pickle), so "an object sneaked across" is
not something a caller can do by accident.

Secrets (the node's management credentials and the customer credential an
action needs) live in exactly two fields, node_secrets and credential.
repr()/str() and redacted() never show them; only to_wire() does, and that
string is written to the runner's private stdin pipe and nowhere else.
"""
from __future__ import annotations

import enum
import json
import math
import uuid
from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Optional

SCHEMA_VERSION = 1
REDACTED = "<redacted>"


class RemoteActionError(ValueError):
    """Base class: the DTO/result is not acceptable."""


class NotSerializable(RemoteActionError, TypeError):
    """A value that is not plain data was put into a DTO or result."""


class SchemaVersionMismatch(RemoteActionError):
    """Parent and child disagree on the contract version."""


class ActionType(str, enum.Enum):
    """The closed list of things the runner may be asked to do. Each real
    one corresponds to rows of the writer inventory (services/
    remote_writers.py). An action_type is a NAME looked up in a registry -
    never a callable or an import path supplied by the caller."""

    WG_ENSURE_PRESENT = "wg_ensure_present"
    WG_ENSURE_ABSENT = "wg_ensure_absent"
    WG_BOUNCE_PEER = "wg_bounce_peer"
    WG_SET_PEER_ENABLED = "wg_set_peer_enabled"
    WG_RENAME_PEER = "wg_rename_peer"
    WG_SET_SPEED_QUEUE = "wg_set_speed_queue"
    WG_RECONCILE_INTERFACE_ADDRESS = "wg_reconcile_interface_address"
    XRAY_ENSURE_PRESENT = "xray_ensure_present"
    XRAY_ENSURE_ABSENT = "xray_ensure_absent"
    XRAY_BOUNCE_CLIENT = "xray_bounce_client"
    XRAY_SET_CLIENT_ENABLED = "xray_set_client_enabled"
    XRAY_REBUILD_CLIENTS = "xray_rebuild_clients"
    SOFTETHER_ENSURE_PRESENT = "softether_ensure_present"
    SOFTETHER_ENSURE_ABSENT = "softether_ensure_absent"
    SOFTETHER_BOUNCE_USER = "softether_bounce_user"
    SOFTETHER_SET_USER_ENABLED = "softether_set_user_enabled"
    PPP_KICK_SESSION = "ppp_kick_session"
    MT_PUSH_RADIUS = "mt_push_radius"
    MT_PUSH_PPTP = "mt_push_pptp"
    MT_PUSH_SSTP = "mt_push_sstp"
    MT_PUSH_L2TP = "mt_push_l2tp"
    MT_PUSH_IKEV2 = "mt_push_ikev2"
    CONTRACT_PROBE = "contract_probe"
    # Runner self-test - no node, no network. Used by the runner's own
    # health check and by the tests.
    SELFTEST_ECHO = "selftest_echo"
    SELFTEST_SLEEP = "selftest_sleep"
    SELFTEST_CRASH = "selftest_crash"
    SELFTEST_ENVIRONMENT = "selftest_environment"


class Outcome(str, enum.Enum):
    SUCCEEDED = "succeeded"
    ALREADY_PRESENT_VERIFIED = "already_present_verified"
    ABSENT_VERIFIED = "absent_verified"
    CONFLICT = "conflict"
    UNREADABLE = "unreadable"
    TRANSPORT_ERROR = "transport_error"
    TIMEOUT_UNKNOWN = "timeout_unknown"
    KILLED_UNKNOWN = "killed_unknown"


REMOTE_OUTCOMES = (None, "verified_absent", "delete_idempotently_absent", "unverified")


def _check_plain(value: Any, path: str) -> None:
    """Raises NotSerializable unless `value` is built only from None, bool,
    int, finite float, str, and lists/dicts (string keys) of those."""
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise NotSerializable(f"{path}: non-finite float")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_plain(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise NotSerializable(f"{path}: dict key {type(key).__name__} is not a string")
            _check_plain(item, f"{path}.{key}")
        return
    raise NotSerializable(f"{path}: {type(value).__module__}.{type(value).__qualname__} is not plain data")


def _plain_copy(value: Any) -> Any:
    """A detached copy made of builtin containers only (tuples -> lists)."""
    if isinstance(value, (list, tuple)):
        return [_plain_copy(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain_copy(v) for k, v in value.items()}
    return value


def _dict_field(value: Any, name: str) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise NotSerializable(f"{name}: expected a mapping, got {type(value).__name__}")
    plain = dict(value)
    _check_plain(plain, name)
    return _plain_copy(plain)


def _canonical_uuid(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise RemoteActionError(f"{name}: expected a UUID string")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError):
        raise RemoteActionError(f"{name}: not a canonical UUID") from None
    return value


@dataclass(frozen=True, repr=False)
class RemoteActionDTO:
    """One remote action. Built by the parent from plain values it extracted
    itself (never from an ORM object passed along) BEFORE the runner is
    spawned."""

    action_type: ActionType
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    node_id: Optional[int] = None
    backend: Optional[str] = None
    node_config: dict = field(default_factory=dict)   # host, port, interface, hub, inbound tag/id ... (not secret)
    node_secrets: dict = field(default_factory=dict)  # the node's management credentials - SECRET
    identity: dict = field(default_factory=dict)      # remote identity of what is acted on (not secret)
    credential: dict = field(default_factory=dict)    # customer credential the action needs - SECRET
    params: dict = field(default_factory=dict)        # non-secret action parameters
    timeouts: dict = field(default_factory=dict)      # connect_timeout, read_timeout, hard_deadline (seconds)
    contract: dict = field(default_factory=dict)      # the node's verified not_exist matchers, adapter_version
    fencing: dict = field(default_factory=dict)       # installation_uuid, host/boot id, epochs, audit ids
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        try:
            action_type = ActionType(self.action_type)
        except ValueError:
            raise RemoteActionError(f"unknown action_type {self.action_type!r}") from None
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "action_id", _canonical_uuid(self.action_id, "action_id"))
        if type(self.schema_version) is not int:
            raise RemoteActionError("schema_version must be an integer")
        if self.node_id is not None and (type(self.node_id) is not int or self.node_id <= 0):
            raise RemoteActionError("node_id must be a positive integer or None")
        if self.backend is not None and not isinstance(self.backend, str):
            raise RemoteActionError("backend must be a string or None")
        for name in ("node_config", "node_secrets", "identity", "credential", "params",
                     "timeouts", "contract", "fencing"):
            object.__setattr__(self, name, _dict_field(getattr(self, name), name))
        for key, value in self.timeouts.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise RemoteActionError(f"timeouts.{key} must be a positive number")

    # -- what may be shown ------------------------------------------------
    def redacted(self) -> dict:
        """Everything except the two secret fields, whose VALUES are
        replaced (their keys stay, so a log still shows what was sent)."""
        out = self._as_dict()
        out["node_secrets"] = {key: REDACTED for key in self.node_secrets}
        out["credential"] = {key: REDACTED for key in self.credential}
        return out

    def __repr__(self) -> str:
        return f"RemoteActionDTO({json.dumps(self.redacted(), sort_keys=True, ensure_ascii=False)})"

    __str__ = __repr__

    # -- the wire ---------------------------------------------------------
    def _as_dict(self) -> dict:
        out = {f.name: _plain_copy(getattr(self, f.name)) for f in fields(self)}
        out["action_type"] = self.action_type.value
        return out

    def to_wire(self) -> str:
        """The full DTO INCLUDING secrets. Only ever written to the runner's
        private pipe."""
        return json.dumps(self._as_dict(), sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_wire(cls, text: str) -> "RemoteActionDTO":
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            raise RemoteActionError("DTO is not valid JSON") from None
        if not isinstance(data, dict):
            raise RemoteActionError("DTO is not a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise SchemaVersionMismatch(
                f"DTO schema_version {data.get('schema_version')!r}, this process speaks {SCHEMA_VERSION}"
            )
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise RemoteActionError(f"DTO has unknown fields: {unknown}")
        return cls(**data)

    @property
    def hard_deadline(self) -> Optional[float]:
        value = self.timeouts.get("hard_deadline")
        return float(value) if value is not None else None


@dataclass(frozen=True)
class RemoteActionResult:
    """What the runner reports. killed_unknown is only ever produced by the
    PARENT (the child cannot report its own death)."""

    action_id: str
    outcome: Outcome
    write_attempted: bool = False
    remote_outcome: Optional[str] = None
    conflict_code: Optional[str] = None
    observed: dict = field(default_factory=dict)   # non-secret values read back
    error_code: Optional[str] = None
    error_sanitized: Optional[str] = None
    duration_ms: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self):
        try:
            outcome = Outcome(self.outcome)
        except ValueError:
            raise RemoteActionError(f"unknown outcome {self.outcome!r}") from None
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "action_id", _canonical_uuid(self.action_id, "action_id"))
        if type(self.write_attempted) is not bool:
            raise RemoteActionError("write_attempted must be a bool")
        if self.remote_outcome not in REMOTE_OUTCOMES:
            raise RemoteActionError(f"unknown remote_outcome {self.remote_outcome!r}")
        for name in ("conflict_code", "error_code", "error_sanitized"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise RemoteActionError(f"{name} must be a string or None")
        if type(self.duration_ms) is not int or self.duration_ms < 0:
            raise RemoteActionError("duration_ms must be a non-negative integer")
        if type(self.schema_version) is not int:
            raise RemoteActionError("schema_version must be an integer")
        object.__setattr__(self, "observed", _dict_field(self.observed, "observed"))

    @property
    def is_unknown(self) -> bool:
        """True when the remote state must be re-read before anything is
        concluded: the action may or may not have taken effect. Never to be
        treated as success, and never as failure."""
        if self.outcome in (Outcome.TIMEOUT_UNKNOWN, Outcome.KILLED_UNKNOWN):
            return True
        return self.outcome is Outcome.TRANSPORT_ERROR and self.write_attempted

    def to_wire(self) -> str:
        out = {f.name: _plain_copy(getattr(self, f.name)) for f in fields(self)}
        out["outcome"] = self.outcome.value
        return json.dumps(out, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_wire(cls, text: str) -> "RemoteActionResult":
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            raise RemoteActionError("result is not valid JSON") from None
        if not isinstance(data, dict):
            raise RemoteActionError("result is not a JSON object")
        if data.get("schema_version") != SCHEMA_VERSION:
            raise SchemaVersionMismatch(
                f"result schema_version {data.get('schema_version')!r}, this process speaks {SCHEMA_VERSION}"
            )
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise RemoteActionError(f"result has unknown fields: {unknown}")
        return cls(**data)

    @classmethod
    def killed_unknown(cls, action_id: str, reason: str, duration_ms: int = 0) -> "RemoteActionResult":
        return cls(action_id=action_id, outcome=Outcome.KILLED_UNKNOWN, write_attempted=True,
                   error_code=reason, duration_ms=duration_ms)
