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


# ------------------------------------------------------------- not-exist matchers
# A node's contract lists the EXACT answers that mean "no such object" on
# that node (design 5.10). There is no wildcard, no pattern, and no
# status-only HTTP matcher: "any 404" also describes a wrong URL, a proxy
# error page or a blocked request.
MATCHER_OPS = ("read", "delete", "list_empty")
MAX_MATCHERS_PER_OP = 4


class InvalidMatcher(ValueError):
    pass


def validate_matchers(matchers) -> list[dict]:
    """Returns the matchers if every one is well-formed, else raises
    InvalidMatcher. Unknown keys are rejected rather than ignored."""
    if not isinstance(matchers, (list, tuple)):
        raise InvalidMatcher("matchers must be a list")
    per_op: dict = {}
    for matcher in matchers:
        if not isinstance(matcher, dict):
            raise InvalidMatcher("a matcher must be an object")
        kind, op = matcher.get("kind"), matcher.get("op")
        if kind == "http":
            allowed = {"kind", "op", "status", "json_path", "equals"}
            if type(matcher.get("status")) is not int:
                raise InvalidMatcher("http matcher needs an exact integer status")
            path = matcher.get("json_path")
            if not isinstance(path, str) or not path or not all(part.isidentifier() for part in path.split(".")):
                raise InvalidMatcher("http matcher needs a plain dotted json_path (a status alone is not enough)")
            if "equals" not in matcher or isinstance(matcher["equals"], (dict, list)) or matcher["equals"] is None:
                raise InvalidMatcher("http matcher needs an exact scalar 'equals'")
        elif kind == "jsonrpc":
            allowed = {"kind", "op", "error_code"}
            if type(matcher.get("error_code")) is not int:
                raise InvalidMatcher("jsonrpc matcher needs an exact integer error_code")
        else:
            raise InvalidMatcher(f"unknown matcher kind {kind!r}")
        if op not in MATCHER_OPS or (kind == "jsonrpc" and op == "list_empty"):
            raise InvalidMatcher(f"unknown matcher op {op!r}")
        unknown = set(matcher) - allowed
        if unknown:
            raise InvalidMatcher(f"unknown matcher keys {sorted(unknown)}")
        per_op[op] = per_op.get(op, 0) + 1
        if per_op[op] > MAX_MATCHERS_PER_OP:
            raise InvalidMatcher(f"more than {MAX_MATCHERS_PER_OP} matchers for {op!r}")
    return list(matchers)


def http_matches(matchers, op: str, status, body) -> bool:
    """True only if (status, body) is exactly one of the node's verified
    answers for `op`. A malformed matcher list matches nothing."""
    try:
        valid = validate_matchers(matchers or [])
    except InvalidMatcher:
        return False
    for matcher in valid:
        if matcher["kind"] != "http" or matcher["op"] != op or matcher["status"] != status:
            continue
        value = body
        for part in matcher["json_path"].split("."):
            value = value.get(part) if isinstance(value, dict) else None
        if value is not None and type(value) is type(matcher["equals"]) and value == matcher["equals"]:
            return True
    return False
