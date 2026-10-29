"""Runner-child bootstrap probe of one node's contract (Lifecycle 5.10).

Runs only in the runner, in 'enforced', under the node lock and the same
fencing as every legacy writer (LegacyChildGuard, phase 'probe'). It never
uses a customer's identity and trusts no answer in advance:

  1. a fresh, namespaced test identity "um-probe-<16 hex>" (+ fresh uuid /
     password / key); never stored anywhere but this probe's memory
  2. read_before   -> projection P1 of the raw "it is not there" answer
  3. create
  4. read           must be present_match on EVERY part of the identity
  5. delete
  6. read_after    -> projection P2
  7. only if P1 and P2 are both complete, valid and EQUAL is a not_exist
     matcher built from them (and, where the backend needs one, from two
     equal answers to deleting the absent identity)

Anything else - timeout, HTML, a partial or non-JSON answer, P1 != P2, a
failed step 4, any doubt in cleanup - is "not ready", with no matcher. If
the test identity may still exist the result says probe_cleanup_unknown and
names it (no uuid, no password) for manual removal.

Returns {"ready", "contract", "server_fingerprint", "reason", "cleanup",
"probe_identity"}; the parent writes the provisioning_node_contracts row.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import uuid

from . import provisioning_child_authority as authority
from .adapter_base import AbsentOutcome, ReadState
from .remote_action import Outcome, RemoteActionResult

HTTP_PATHS = ("detail", "message", "msg", "error", "error.message", "error.code")


class ProbeFailed(Exception):
    def __init__(self, reason: str, cleanup: str = "clean"):
        super().__init__(reason)
        self.reason, self.cleanup = reason, cleanup


def _shape(value, depth=0):
    """Keys and types only, never values - the 'shape' fingerprint (5.10)."""
    if depth > 3:
        return type(value).__name__
    if isinstance(value, dict):
        return {key: _shape(value[key], depth + 1) for key in sorted(value)}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    return type(value).__name__


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _projection(status, body):
    """(status, json_path, value) for an HTTP 'not there' answer, or None."""
    if type(status) is not int or not isinstance(body, dict):
        return None
    for path in HTTP_PATHS:
        value = body
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if type(value) in (str, int) and value != "":
            return (status, path, value)
    return None


def _matcher(op, first, second):
    if first is None or first != second:
        raise ProbeFailed(f"{op}_not_exist_unstable")
    status, path, value = first
    return {"kind": "http", "op": op, "status": status, "json_path": path, "equals": value}


def _name():
    return "um-probe-" + secrets.token_hex(8)


# ------------------------------------------------------------------ per backend
def _probe_mikrotik(mt, binding, config, state):
    from . import adapter_mikrotik_wg as wg
    from .keys import generate_wireguard_keypair
    interface = config.get("mt_wireguard_interface")
    if not interface:
        raise ProbeFailed("wireguard_interface_missing")
    resources = list(mt._api.path("system", "resource"))
    version = resources[0].get("version") if resources else None
    if not version:
        raise ProbeFailed("server_version_unreadable")
    try:
        # RouterOS does not create this interface automatically. Match the
        # normal WG provisioning path: create it only when absent, under this
        # probe's runner-child guard and node lock, using the configured port.
        mt.ensure_wireguard_interface(interface, config.get("mt_endpoint_port") or 13231)
    except Exception:  # noqa: BLE001
        raise ProbeFailed("wireguard_interface_create_failed") from None
    if not any(row.get("name") == interface for row in mt._api.path("interface", "wireguard")):
        raise ProbeFailed("wireguard_interface_not_ready")
    _private, public = generate_wireguard_keypair()
    name = state["name"] = _name()
    identity = wg.WireguardIdentity(interface=interface, peer_name=name, public_key=public,
                                    client_address=f"192.0.2.{secrets.randbelow(254) + 1}/32")
    if wg.read(mt, identity).state is not ReadState.ABSENT:
        raise ProbeFailed("read_before_not_absent")
    state["create_attempted"] = True
    try:
        mt.add_peer(interface, public_key=public, allowed_address=identity.client_address, comment=name)
    except Exception:  # noqa: BLE001
        raise ProbeFailed("create_failed", "unknown") from None
    if wg.read(mt, identity).state is not ReadState.PRESENT_MATCH:
        raise ProbeFailed("read_after_create_not_match", "unknown")
    if wg.ensure_absent(mt, identity) is not AbsentOutcome.VERIFIED_ABSENT:
        raise ProbeFailed("delete_unverified", "unknown")
    if wg.read(mt, identity).state is not ReadState.ABSENT:
        raise ProbeFailed("read_after_delete_not_absent", "unknown")
    return name, {"not_exist": []}, _fingerprint({"routeros": version})


def _probe_xray(xc, binding, config, state):
    from . import adapter_xray as ax
    name = state["name"] = _name() + "@usermanager.local"
    ssh = binding.backend == "xray_ssh"
    identity = ax.XrayIdentity(email=name, uuid=str(uuid.uuid4()), inbound_tag=config.get("xr_inbound_tag"),
                               inbound_id=config.get("xr_panel_inbound_id"))
    read = ax.read_ssh if ssh else ax.read_threexui
    if read(xc, identity).state is not ReadState.ABSENT:
        raise ProbeFailed("read_before_not_absent")
    entries = xc.get_inbound_clients_strict(identity.inbound_tag) if ssh else xc.get_inbound_clients_strict()
    state["create_attempted"] = True
    try:
        (ax.ensure_present_ssh(xc, identity) if ssh else ax.ensure_present_threexui(xc, identity))
    except Exception:  # noqa: BLE001
        raise ProbeFailed("create_failed", "unknown") from None
    if read(xc, identity).state is not ReadState.PRESENT_MATCH:
        raise ProbeFailed("read_after_create_not_match", "unknown")
    outcome = ax.ensure_absent_ssh(xc, identity) if ssh else ax.ensure_absent_threexui(xc, identity)
    if outcome is not AbsentOutcome.VERIFIED_ABSENT or read(xc, identity).state is not ReadState.ABSENT:
        raise ProbeFailed("delete_unverified", "unknown")
    return name, {"not_exist": []}, _fingerprint({"clients": _shape(entries)})


def _probe_panel(xc, binding, config, state):
    from . import adapter_xray_panels as ap
    from .marzneshin_client import sanitize_username
    name = _name()
    email = state["name"] = name + "@usermanager.local"
    identity = ap.PanelIdentity(email=email, uuid=str(uuid.uuid4()), inbound_tag=config.get("xr_inbound_tag"),
                                inbound_id=config.get("xr_panel_inbound_id"),
                                username=sanitize_username(email) if binding.backend == "marzneshin" else None)
    backend = binding.backend
    matchers, shape = [], None
    if backend == "marzban":
        status, body = xc.get_user_raw(identity.email)
        first, shape = _projection(status, body), _shape(body)
        if first is None:
            raise ProbeFailed("read_before_not_a_complete_answer")
        # Provisional: lets the adapter read "absent" until the second
        # observation either confirms it (then it is kept) or not (not ready).
        matchers = [{"kind": "http", "op": "read", "status": first[0], "json_path": first[1], "equals": first[2]}]
        create = lambda: xc.create_user_only(identity.email, identity.uuid, identity.flow)
        read = lambda m: ap.read_marzban(xc, identity, m)
        delete = lambda: xc.delete_user_raw(identity.email)
    elif backend == "hiddify":
        status, body = xc.list_users_raw()
        shape = _shape(body)
        if status == 200 and isinstance(body, list):
            first = None
        else:
            first = _projection(status, body)
            if first is None:
                raise ProbeFailed("read_before_not_a_complete_answer")
            matchers.append(_matcher("list_empty", first, _projection(*xc.list_users_raw())))
        create = lambda: xc.create_user_only(identity.email, identity.uuid)
        read = lambda m: ap.read_hiddify(xc, identity, m)
        delete = lambda: xc.delete_user_raw(identity.uuid)
        first = "list"
    elif backend == "marzneshin":
        users = xc.list_users_strict()
        shape, first = _shape(users), "list"
        create = lambda: xc.create_user_only(identity.username, identity.email, identity.uuid)
        read = lambda m: ap.read_marzneshin(xc, identity, m)
        delete = lambda: xc.delete_user_raw(identity.username)
    elif backend == "sui":
        records = xc.find_clients_strict(identity.email)
        if records:
            raise ProbeFailed("read_before_not_absent")
        shape, first = _shape(records), "list"
        create = lambda: xc.create_client_only(identity.email, identity.uuid, identity.flow)
        read = lambda m: ap.read_sui(xc, identity, m)
        delete = None
    else:
        raise ProbeFailed("backend_unsupported")
    if read(matchers).state is not ReadState.ABSENT:
        raise ProbeFailed("read_before_not_absent")
    state["create_attempted"] = True
    try:
        create()
    except Exception:  # noqa: BLE001
        raise ProbeFailed("create_failed", "unknown") from None
    found = read(matchers)
    if found.state is not ReadState.PRESENT_MATCH:
        raise ProbeFailed("read_after_create_not_match", "unknown")
    if delete is None:
        xc.delete_client_by_id(found.handle.get("id"))
    else:
        delete()
    if backend == "marzban":
        second = _projection(*xc.get_user_raw(identity.email))
        matchers = [_matcher("read", first, second)]
    if read(matchers).state is not ReadState.ABSENT:
        raise ProbeFailed("read_after_delete_not_absent", "unknown")
    if delete is not None:
        # Two answers to deleting what is gone, compared: the only basis for
        # a delete-side "already absent" matcher.
        matchers.append(_matcher("delete", _projection(*delete()), _projection(*delete())))
    return email, {"not_exist": matchers}, _fingerprint({"read": shape})


def _probe_softether(sc, binding, config, state):
    from . import adapter_softether as ase
    from .softether_client import SoftEtherError
    name = state["name"] = _name()
    identity = ase.SoftEtherIdentity(username=name, password=secrets.token_urlsafe(18))
    status = sc._call("GetHubStatus", {"HubName_str": sc.hub_name})
    if ase.read(sc, identity).state is not ReadState.ABSENT:
        raise ProbeFailed("read_before_not_absent")
    state["create_attempted"] = True
    try:
        sc.add_client(identity.username, identity.password)
    except Exception:  # noqa: BLE001
        raise ProbeFailed("create_failed", "unknown") from None
    if ase.read(sc, identity).state is not ReadState.PRESENT_MATCH:
        raise ProbeFailed("read_after_create_not_match", "unknown")
    if ase.ensure_absent(sc, identity) is not AbsentOutcome.VERIFIED_ABSENT:
        raise ProbeFailed("delete_unverified", "unknown")
    codes = []
    for _ in range(2):
        try:
            sc._call("DeleteUser", {"HubName_str": sc.hub_name, "Name_str": identity.username})
            codes.append(None)
        except SoftEtherError as exc:
            codes.append(getattr(exc, "rpc_code", None))
    matchers = []
    if codes[0] is not None and codes[0] == codes[1] and type(codes[0]) is int:
        matchers.append({"kind": "jsonrpc", "op": "delete", "error_code": codes[0]})
    return name, {"not_exist": matchers}, _fingerprint({"hub": _shape(status)})


PROBES = {"mikrotik": _probe_mikrotik, "softether": _probe_softether}


def run(dto):
    """Registered runner entry for contract probes and cleanup checks."""
    from .remote_action import ActionType
    from . import provisioning_child_legacy as legacy
    from .provisioning_child_database import ChildDatabase
    from .provisioning_child_entry import _holds as held
    from .provisioning_contract_rules import validate_contract
    from .provisioning_host import read_identity
    authority._role()
    holds = held.get()
    if holds is None or holds[:2] != (os.getpid(), threading.get_ident()):
        raise legacy.LegacyGuardUnavailable("probe_locks_missing")
    trusted_url = os.environ.get("DATABASE_URL", "").strip()
    if not trusted_url:
        raise legacy.LegacyGuardUnavailable("probe_configuration_missing")
    binding = legacy.LegacyBinding(**dto.fencing["binding"])
    if dto.action_type == ActionType.CONTRACT_PROBE_CLEANUP_CHECK:
        return _check_mikrotik_probe_cleanup(dto, binding, holds, legacy, ChildDatabase, read_identity, authority)
    if binding.phase != "probe" or (binding.node_id, binding.backend) != (dto.node_id, dto.backend):
        raise legacy.LegacyGuardUnavailable("probe_binding_invalid")
    kind = "mikrotik" if binding.backend == "mikrotik_wg" else "softether" if binding.backend == "softether" else "xray"
    database = ChildDatabase(trusted_url)
    observed = {"ready": False, "contract": None, "server_fingerprint": None, "reason": None,
                "cleanup": "clean", "probe_identity": None}
    try:
        with legacy.LegacyChildGuard(database, binding, read_identity(), holds[2], holds[3]) as guard:
            with authority._scope(guard):
                client = legacy._client(guard, kind, dto.node_config, dto.node_secrets)
                with client:
                    if kind == "xray":
                        probe = _probe_xray if binding.backend in ("xray_ssh", "threexui") else _probe_panel
                    else:
                        probe = PROBES[kind]
                    state = {}
                    try:
                        name, contract, server = probe(client, binding, dto.node_config, state)
                        validate_contract(json.dumps(contract), binding.backend)
                        observed.update(ready=True, contract=contract, server_fingerprint=server, probe_identity=name)
                    except ProbeFailed as failure:
                        observed.update(reason=failure.reason, cleanup=failure.cleanup)
                    except Exception as exc:  # noqa: BLE001 - any doubt is "not ready", cleanup unknown
                        observed.update(reason=f"probe_error:{type(exc).__name__}",
                                        cleanup="unknown" if state.get("create_attempted") else "clean")
                    if observed["cleanup"] == "unknown":
                        observed["probe_identity"] = state.get("name")
        if observed["cleanup"] == "unknown":
            observed["reason"] = observed["reason"] or "probe_cleanup_unknown"
        return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED, write_attempted=True,
                                  observed=observed)
    finally:
        database.dispose()


def _check_mikrotik_probe_cleanup(dto, binding, holds, legacy, ChildDatabase, read_identity, authority):
    """Read one stored random probe comment under runner locks; never write."""
    from .remote_action import ActionType, Outcome, RemoteActionResult
    identity = dto.params.get("probe_identity") if isinstance(dto.params, dict) else None
    if (dto.action_type != ActionType.CONTRACT_PROBE_CLEANUP_CHECK or binding.phase != "probe_cleanup" or
            (binding.node_id, binding.backend) != (dto.node_id, dto.backend) or
            binding.backend != "mikrotik_wg" or not isinstance(identity, str) or
            not re.fullmatch(r"um-probe-[0-9a-f]{16}", identity)):
        raise legacy.LegacyGuardUnavailable("probe_cleanup_binding_invalid")
    database = ChildDatabase(os.environ["DATABASE_URL"].strip())
    try:
        with legacy.LegacyChildGuard(database, binding, read_identity(), holds[2], holds[3]) as guard:
            with authority._scope(guard, allow_writes=False):
                config = dto.node_config
                interface = config.get("mt_wireguard_interface") if isinstance(config, dict) else None
                if not isinstance(interface, str) or not interface:
                    raise legacy.LegacyGuardUnavailable("probe_cleanup_interface_invalid")
                client = legacy._client(guard, "mikrotik", config, dto.node_secrets)
                with client:
                    # Search the whole node, not only the currently configured
                    # interface: an operator may have moved the exact probe peer
                    # since cleanup became uncertain. Absence must be global to
                    # this MikroTik before we release the retry block.
                    peers = client.list_peers()
                if not isinstance(peers, list) or len(peers) >= 10000:
                    status = "ambiguous"
                else:
                    matches = [peer for peer in peers if isinstance(peer, dict) and peer.get("comment") == identity]
                    status = "absent" if not matches else "present" if len(matches) == 1 else "ambiguous"
        return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED, write_attempted=False,
                                  observed={"status": status, "write_attempted": False})
    finally:
        database.dispose()
