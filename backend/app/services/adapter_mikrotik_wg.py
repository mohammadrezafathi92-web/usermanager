"""WireGuard-on-MikroTik adapter (Lifecycle/P6 design v7.1, section 9.1).

Identity of a peer is the four-tuple (interface, comment, public key,
allowed address). A peer that matches only part of it is a CONFLICT and is
never modified or removed - today's code finds a peer by comment alone,
which is how an unrelated peer could be deleted or an orphan left behind.

Also here: the interface-address contract. The panel's client subnet
(Node.mt_client_subnet) only ever grows, and the router has to follow it.
ensure_interface_address_exact_or_wider() moves the router's address
toward the subnet it is GIVEN and only ever widens it; it never narrows
the router, whatever an older caller believes the subnet to be.
"""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Optional

from .adapter_base import (
    AbsentOutcome,
    AdapterConflict,
    AdapterError,
    AdapterPermanentError,
    PresentResult,
    ReadResult,
    ReadState,
)

ADDRESS_IN_SYNC, ADDRESS_ADDED, ADDRESS_REPAIRED, ADDRESS_ROUTER_WIDER = (
    "in_sync", "added", "repaired", "router_wider",
)


@dataclass(frozen=True)
class WireguardIdentity:
    interface: str
    peer_name: str            # stored as the peer's comment on RouterOS
    public_key: str
    client_address: str       # "10.66.66.2/32" or several, comma-joined
    speed_limit_mbps: Optional[int] = None
    listen_port: int = 13231


def speed_queue_name(peer_name: str) -> str:
    """Must stay identical to user_ops.wg_speed_queue_name (a test pins it):
    both the legacy code and this adapter have to agree on which queue
    belongs to which peer."""
    return f"um-speed-{peer_name}"


def _addresses(value) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def read(mt, identity: WireguardIdentity) -> ReadResult:
    try:
        peers = mt.list_peers(identity.interface)
    except Exception:  # noqa: BLE001 - any failure to read is "unreadable", never "absent"
        return ReadResult(ReadState.UNREADABLE)
    return _classify_peers(peers, identity)


def _classify_peers(peers, identity):

    by_key = [p for p in peers if p.get("public-key") == identity.public_key]
    by_name = [p for p in peers if p.get("comment") == identity.peer_name]
    candidates = {id(p): p for p in by_key + by_name}
    if not candidates:
        return ReadResult(ReadState.ABSENT)
    if len(candidates) > 1:
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_multiple_peers")
    peer = next(iter(candidates.values()))
    if peer.get("public-key") != identity.public_key:
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_name_taken")
    if peer.get("comment") != identity.peer_name:
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_public_key_taken")
    if _addresses(peer.get("allowed-address")) != _addresses(identity.client_address):
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_address_mismatch")
    return ReadResult(ReadState.PRESENT_MATCH, handle=peer.get(".id"))


@dataclass(frozen=True)
class StoppedUsage:
    """RouterOS tx is customer download; rx is customer upload."""
    download_bytes: int
    upload_bytes: int

    def __post_init__(self):
        if any(type(v) is not int or v < 0 for v in (self.download_bytes, self.upload_bytes)):
            raise ValueError("invalid stopped usage")


def _counter(value):
    # Missing/negative/malformed counters are not zero usage. Never let a
    # failed measurement turn into a larger monetary refund.
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        return int(value)
    raise AdapterError("wg_refund_counters_unreadable")


def _stopped_usage(mt, identity):
    try:
        peers = mt.list_peers(identity.interface)
    except Exception:
        raise AdapterError("wg_refund_unreadable") from None
    found = _classify_peers(peers, identity)
    if (found.state is not ReadState.PRESENT_MATCH
            or not isinstance(found.handle, str) or not found.handle):
        raise AdapterError("wg_refund_identity_unverified")
    peer = next(p for p in peers if p.get(".id") == found.handle)
    if peer.get("disabled") not in (True, "true", "yes"):
        raise AdapterError("wg_refund_stop_unconfirmed")
    return StoppedUsage(_counter(peer.get("tx")), _counter(peer.get("rx")))


def stop_capture_remove(mt, identity: WireguardIdentity, *, persist_usage,
                        previously_captured: StoppedUsage | None = None) -> StoppedUsage:
    """Refund worker primitive, NOT a live legacy deletion replacement.

    Caller must fence ALL writers from before this call through settlement.
    persist_usage must commit the counters and return the exact same value
    before this function may delete. An absent peer with no durable sample
    is irrecoverably unmeasured, NOT free. The interface is never changed.
    """
    if previously_captured is not None and not isinstance(previously_captured, StoppedUsage):
        raise AdapterError("wg_refund_usage_missing")
    found = read(mt, identity)
    if found.state is ReadState.PRESENT_CONFLICT:
        raise AdapterConflict(found.conflict_code or "wg_refund_identity_conflict")
    if found.state is ReadState.UNREADABLE:
        raise AdapterError("wg_refund_unreadable")
    if found.state is ReadState.ABSENT:
        if previously_captured is None:
            raise AdapterError("wg_refund_usage_missing")
        measured = previously_captured
    else:
        if not isinstance(found.handle, str) or not found.handle:
            raise AdapterError("wg_refund_identity_unverified")
        try:
            mt.set_peer_disabled(found.handle, disabled=True)
        except Exception:
            raise AdapterError("wg_refund_stop_failed") from None
        measured = _stopped_usage(mt, identity)
        if _stopped_usage(mt, identity) != measured:
            raise AdapterError("wg_refund_counters_not_stable")
        if previously_captured is not None and previously_captured != measured:
            raise AdapterError("wg_refund_counters_changed")
        if persist_usage(measured) != measured:
            raise AdapterError("wg_refund_usage_not_persisted")
    # Includes a second exact-identity read, post-delete absence check and
    # removal of the peer's own speed queue. Failure leaves settlement blocked.
    if ensure_absent(mt, identity) is not AbsentOutcome.VERIFIED_ABSENT:
        raise AdapterError("wg_refund_delete_unconfirmed")
    return measured


def ensure_interface_address_exact_or_wider(mt, interface: str, desired_subnet: str) -> str:
    """Makes the router's address on `interface` cover `desired_subnet`.

    router has no address            -> add the gateway address      (added)
    router == desired                -> nothing                      (in_sync)
    router narrower than desired     -> replace with desired         (repaired)
    router WIDER than desired        -> nothing, never narrowed      (router_wider)
    unrelated network / several rows -> AdapterPermanentError, nothing changed
    """
    desired = ipaddress.ip_network(desired_subnet, strict=False)
    desired_address = f"{desired.network_address + 1}/{desired.prefixlen}"

    def current():
        rows = mt.list_interface_addresses(interface)
        if len(rows) > 1:
            raise AdapterPermanentError("wg_interface_address_conflict", "interface has more than one address")
        if not rows:
            return None
        try:
            return ipaddress.ip_interface(rows[0].get("address")).network
        except ValueError:
            raise AdapterPermanentError("wg_interface_address_conflict", "unparseable interface address") from None

    try:
        network = current()
        if network is None:
            mt.ensure_interface_address(interface, desired_address)
            return ADDRESS_ADDED
        if network == desired:
            return ADDRESS_IN_SYNC
        if network.version == desired.version and network.subnet_of(desired):
            # Look again right before replacing: only widen what is still narrower.
            again = current()
            if again is None or (again != desired and again.subnet_of(desired)):
                mt.set_interface_address(interface, desired_address)
                return ADDRESS_REPAIRED
            return ADDRESS_IN_SYNC if again == desired else ADDRESS_ROUTER_WIDER
        if network.version == desired.version and desired.subnet_of(network):
            return ADDRESS_ROUTER_WIDER
        raise AdapterPermanentError("wg_interface_address_conflict", "router network unrelated to the panel's subnet")
    except AdapterError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AdapterError("wg_interface_address_failed", type(exc).__name__) from None


def ensure_present(mt, identity: WireguardIdentity, desired_subnet: str) -> PresentResult:
    """desired_subnet is the node's CURRENT client subnet, read by the
    caller after it took the node gate - never a value remembered from when
    the step was created."""
    notes = []
    try:
        mt.ensure_wireguard_interface(identity.interface, identity.listen_port)
    except Exception as exc:  # noqa: BLE001
        raise AdapterError("wg_interface_failed", type(exc).__name__) from None
    notes.append(f"address:{ensure_interface_address_exact_or_wider(mt, identity.interface, desired_subnet)}")

    subnet = ipaddress.ip_network(desired_subnet, strict=False)
    for address in _addresses(identity.client_address):
        try:
            inside = ipaddress.ip_interface(address).ip in subnet
        except ValueError:
            inside = False
        if not inside:
            raise AdapterPermanentError("wg_ip_outside_subnet", "client address is outside the node's subnet")

    found = read(mt, identity)
    if found.state is ReadState.PRESENT_CONFLICT:
        raise AdapterConflict(found.conflict_code or "remote_identity_conflict")
    if found.state is ReadState.UNREADABLE:
        raise AdapterError("wg_unreadable")
    created = False
    if found.state is ReadState.ABSENT:
        try:
            mt.add_peer(identity.interface, public_key=identity.public_key,
                        allowed_address=identity.client_address, comment=identity.peer_name)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError("wg_add_peer_failed", type(exc).__name__) from None
        created = True
        if read(mt, identity).state is not ReadState.PRESENT_MATCH:
            raise AdapterError("wg_add_peer_unconfirmed")

    if identity.speed_limit_mbps:
        try:
            mt.upsert_simple_queue(speed_queue_name(identity.peer_name), identity.client_address,
                                   identity.speed_limit_mbps)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError("wg_queue_failed", type(exc).__name__) from None
    return PresentResult(created=created, notes=tuple(notes))


def ensure_absent(mt, identity: WireguardIdentity) -> AbsentOutcome:
    """Never touches the interface or its address."""
    found = read(mt, identity)
    if found.state in (ReadState.PRESENT_CONFLICT, ReadState.UNREADABLE):
        return AbsentOutcome.UNVERIFIED
    if found.state is ReadState.PRESENT_MATCH:
        if not isinstance(found.handle, str) or not found.handle:
            return AbsentOutcome.UNVERIFIED
        try:
            mt.remove_peer(found.handle)
        except Exception:  # noqa: BLE001
            return AbsentOutcome.UNVERIFIED
        if read(mt, identity).state is not ReadState.ABSENT:
            return AbsentOutcome.UNVERIFIED
    try:
        mt.remove_simple_queue(speed_queue_name(identity.peer_name))
    except Exception:  # noqa: BLE001
        return AbsentOutcome.UNVERIFIED
    return AbsentOutcome.VERIFIED_ABSENT
