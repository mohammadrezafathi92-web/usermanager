"""Shared vocabulary of the per-backend provisioning adapters
(Lifecycle/P6 design v7.1, section 9).

Every backend adapter offers the same three operations on a client it is
GIVEN (it never builds one, never takes a lock, never touches the
database):

    read(client, identity)            -> ReadResult
    ensure_present(client, identity)  -> PresentResult   (or raises)
    ensure_absent(client, identity)   -> AbsentOutcome

The identity and the credential always come from the caller's stored step -
an adapter never generates either. `present_conflict` means "something on
the node shares PART of this identity": the adapter then changes and
deletes NOTHING.

Nothing calls these adapters yet; they are exercised by tests with fake
clients until the durable path is wired to them.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field


class ReadState(str, enum.Enum):
    PRESENT_MATCH = "present_match"
    PRESENT_CONFLICT = "present_conflict"
    ABSENT = "absent"
    UNREADABLE = "unreadable"


class AbsentOutcome(str, enum.Enum):
    """Same three words as the design's remote_outcome ('abandoned' is only
    ever written by a human force, never by an adapter)."""
    VERIFIED_ABSENT = "verified_absent"
    DELETE_IDEMPOTENTLY_ABSENT = "delete_idempotently_absent"
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class ReadResult:
    state: ReadState
    conflict_code: str | None = None
    # Whatever the adapter needs to act on the SAME object within the same
    # gate hold (a RouterOS .id, a panel row id). Never stored, never logged.
    handle: object = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class PresentResult:
    created: bool          # False: it was already there and matched
    notes: tuple[str, ...] = ()


class AdapterError(Exception):
    """An adapter could not finish. `code` is a fixed identifier safe to
    store; the message never contains a credential."""

    retryable = True

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


class AdapterPermanentError(AdapterError):
    """Retrying will not help (the node's own state contradicts the step)."""
    retryable = False


class AdapterConflict(AdapterPermanentError):
    """read() found present_conflict - nothing was changed."""
