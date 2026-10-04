"""SoftEther adapter (Lifecycle/P6 design v7.1, section 9.5).

Identity is (hub, username). The hub never returns a password, so there is
no "present with a different credential" state here: an existing user is
converged onto the staged password with SetUser.

Removal follows the three outcomes strictly:

    EnumUser answered and the user is not in it      -> verified_absent
    DeleteUser answered a verified "no such user"
      code AND the hub could not be listed           -> delete_idempotently_absent
    anything else                                    -> unverified

"DeleteUser raised nothing" is NOT evidence of absence on its own.
"""
from __future__ import annotations

from dataclasses import dataclass

from .adapter_base import AbsentOutcome, AdapterError, PresentResult, ReadResult, ReadState


@dataclass(frozen=True, repr=False)
class SoftEtherIdentity:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"SoftEtherIdentity(username={self.username!r})"


def read(sc, identity: SoftEtherIdentity) -> ReadResult:
    try:
        return ReadResult(ReadState.PRESENT_MATCH if sc.user_exists(identity.username) else ReadState.ABSENT)
    except Exception:  # noqa: BLE001
        return ReadResult(ReadState.UNREADABLE)


def ensure_present(sc, identity: SoftEtherIdentity) -> PresentResult:
    found = read(sc, identity)
    if found.state is ReadState.UNREADABLE:
        raise AdapterError("softether_unreadable")
    if found.state is ReadState.PRESENT_MATCH:
        # A retry after an ambiguous CreateUser lands here: re-send the
        # staged password so the hub and the panel agree on it.
        try:
            applied = sc.set_client_enabled(identity.username, identity.password, True)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError("softether_set_failed", type(exc).__name__) from None
        if not applied:
            raise AdapterError("softether_set_unconfirmed")
        return PresentResult(created=False)
    try:
        sc.add_client(identity.username, identity.password)
    except Exception as exc:  # noqa: BLE001
        raise AdapterError("softether_add_failed", type(exc).__name__) from None
    if read(sc, identity).state is not ReadState.PRESENT_MATCH:
        raise AdapterError("softether_add_unconfirmed")
    return PresentResult(created=True)


def ensure_absent(sc, identity: SoftEtherIdentity, not_exist_codes=()) -> AbsentOutcome:
    """not_exist_codes: JSON-RPC error codes verified ON THIS NODE to mean
    "no such user" (the node's contract). Empty by default, in which case
    only a successful listing can prove absence."""
    try:
        deleted = sc.delete_user_typed(identity.username, not_exist_codes)
    except Exception:  # noqa: BLE001
        deleted = "ambiguous"
    listing = read(sc, identity)
    if listing.state is ReadState.ABSENT:
        return AbsentOutcome.VERIFIED_ABSENT
    if listing.state is ReadState.UNREADABLE and deleted == "not_exist":
        return AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT
    return AbsentOutcome.UNVERIFIED
