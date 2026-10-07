"""Adapters for Xray clients reached over SSH (config.json) and through a
3X-UI panel (Lifecycle/P6 design v7.1, sections 9.3 and 9.4).

Identity of a client is (inbound, email, uuid). The uuid is a credential:
it is compared here, never logged, and never put in an error.

The legacy add_client of both backends is safe to call only AFTER read()
said `absent` (XrayClient.add_client replaces any entry with the same
email; here there is none). What this module never does is "write, and if
that fails write differently" - the read decides, the write follows, and a
second read confirms.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .adapter_base import (
    AbsentOutcome,
    AdapterConflict,
    AdapterError,
    PresentResult,
    ReadResult,
    ReadState,
)


@dataclass(frozen=True, repr=False)
class XrayIdentity:
    email: str
    uuid: str
    flow: str = ""
    inbound_tag: Optional[str] = None       # xray_ssh
    inbound_id: Optional[int] = None        # threexui

    def __repr__(self) -> str:              # the uuid must never reach a log
        return f"XrayIdentity(email={self.email!r}, inbound_tag={self.inbound_tag!r}, inbound_id={self.inbound_id!r})"


def _classify(entries, identity: XrayIdentity, uuid_of: Callable[[dict], Optional[str]]) -> ReadResult:
    same_email = [c for c in entries if isinstance(c, dict) and c.get("email") == identity.email]
    if not same_email:
        return ReadResult(ReadState.ABSENT)
    if len(same_email) > 1:
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_duplicate_email")
    if uuid_of(same_email[0]) != identity.uuid:
        return ReadResult(ReadState.PRESENT_CONFLICT, "conflict_uuid_mismatch")
    return ReadResult(ReadState.PRESENT_MATCH, handle=same_email[0])


def read_ssh(xc, identity: XrayIdentity) -> ReadResult:
    try:
        entries = xc.get_inbound_clients_strict(identity.inbound_tag)
    except Exception:  # noqa: BLE001
        return ReadResult(ReadState.UNREADABLE)
    # vless/vmess keep the uuid in "id", trojan in "password".
    return _classify(entries, identity, lambda c: c.get("id") or c.get("password"))


def read_threexui(xc, identity: XrayIdentity) -> ReadResult:
    try:
        entries = xc.get_inbound_clients_strict()
    except Exception:  # noqa: BLE001
        return ReadResult(ReadState.UNREADABLE)
    return _classify(entries, identity, lambda c: c.get("id"))


def _ensure_present(xc, identity, read, prefix) -> PresentResult:
    found = read(xc, identity)
    if found.state is ReadState.PRESENT_MATCH:
        # Already there: no write at all - for xray_ssh that also means no
        # config rewrite and no service restart on a retry.
        return PresentResult(created=False)
    if found.state is ReadState.PRESENT_CONFLICT:
        raise AdapterConflict(found.conflict_code or "remote_identity_conflict")
    if found.state is ReadState.UNREADABLE:
        raise AdapterError(f"{prefix}_unreadable")
    try:
        xc.add_client(identity.inbound_tag, identity.email, identity.uuid, identity.flow)
    except Exception as exc:  # noqa: BLE001
        raise AdapterError(f"{prefix}_add_failed", type(exc).__name__) from None
    # The write's own answer is not the evidence - the read after it is.
    if read(xc, identity).state is not ReadState.PRESENT_MATCH:
        raise AdapterError(f"{prefix}_add_unconfirmed")
    return PresentResult(created=True)


def _ensure_absent(xc, identity, read) -> AbsentOutcome:
    found = read(xc, identity)
    if found.state is ReadState.ABSENT:
        return AbsentOutcome.VERIFIED_ABSENT      # nothing written, nothing restarted
    if found.state is not ReadState.PRESENT_MATCH:
        return AbsentOutcome.UNVERIFIED           # conflict or unreadable: delete nothing
    try:
        xc.remove_client(identity.inbound_tag, identity.email, identity.uuid)
    except Exception:  # noqa: BLE001
        return AbsentOutcome.UNVERIFIED
    return AbsentOutcome.VERIFIED_ABSENT if read(xc, identity).state is ReadState.ABSENT else AbsentOutcome.UNVERIFIED


def ensure_present_ssh(xc, identity: XrayIdentity, *, confirm_restart=False) -> PresentResult:
    if type(confirm_restart) is not bool:
        raise AdapterError("xray_ssh_restart_policy_invalid")
    result = _ensure_present(xc, identity, read_ssh, "xray_ssh")
    if confirm_restart and not result.created:
        # An ambiguous prior config write can precede an unfinished restart.
        # Only converge after _ensure_present proves the full stored identity.
        try:
            xc.restart_service()
        except Exception:
            raise AdapterError("xray_ssh_restart_unconfirmed") from None
    return result


def ensure_absent_ssh(xc, identity: XrayIdentity) -> AbsentOutcome:
    return _ensure_absent(xc, identity, read_ssh)


def ensure_present_threexui(xc, identity: XrayIdentity) -> PresentResult:
    return _ensure_present(xc, identity, read_threexui, "threexui")


def ensure_absent_threexui(xc, identity: XrayIdentity) -> AbsentOutcome:
    return _ensure_absent(xc, identity, read_threexui)
