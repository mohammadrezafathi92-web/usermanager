"""WireGuard cancellation/refund steps (unpaid-service cancellation).

Deliberately NOT one of the node-contract files hashed by
provisioning_contract_rules: changing these steps must never invalidate a
verified node contract (receipt 1140). They build only on the contract
adapter's own identity read (read / _classify_peers) and verified absence
(ensure_absent), which stay in adapter_mikrotik_wg.py and stay hashed.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapter_base import AbsentOutcome, AdapterConflict, AdapterError, ReadState
from .adapter_mikrotik_wg import WireguardIdentity, _classify_peers, ensure_absent, read


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


def stop_and_measure(mt, identity: WireguardIdentity,
                     previously_captured: StoppedUsage | None = None) -> StoppedUsage:
    """Refund step 1 of 2: disable the exact peer and read its final
    counters twice (they must be stable). An absent peer is answered only
    by a sample captured before (a resume); otherwise it is unmeasured,
    NOT free. The caller persists the result before step 2."""
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
        return previously_captured
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
    return measured


def remove_stopped(mt, identity: WireguardIdentity, persisted: StoppedUsage) -> None:
    """Refund step 2 of 2: delete the peer whose final counters were
    persisted. A peer still present must still be disabled with exactly
    those counters (nothing used it since); then the same verified
    absence as ensure_absent (second exact-identity read, post-delete
    check, the peer's own speed queue). Failure leaves settlement blocked."""
    if not isinstance(persisted, StoppedUsage):
        raise AdapterError("wg_refund_usage_missing")
    if read(mt, identity).state is ReadState.PRESENT_MATCH and _stopped_usage(mt, identity) != persisted:
        raise AdapterError("wg_refund_counters_changed")
    if ensure_absent(mt, identity) is not AbsentOutcome.VERIFIED_ABSENT:
        raise AdapterError("wg_refund_delete_unconfirmed")


def stop_capture_remove(mt, identity: WireguardIdentity, *, persist_usage,
                        previously_captured: StoppedUsage | None = None) -> StoppedUsage:
    """Both refund steps on one client, persist_usage between them (it must
    commit the counters and return the exact same value before anything is
    deleted). Caller must fence ALL writers from before this call through
    settlement. The interface is never changed."""
    measured = stop_and_measure(mt, identity, previously_captured)
    if (previously_captured is None or measured is not previously_captured) and persist_usage(measured) != measured:
        raise AdapterError("wg_refund_usage_not_persisted")
    remove_stopped(mt, identity, measured)
    return measured
