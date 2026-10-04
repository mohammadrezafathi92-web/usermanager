"""Adapters for the four Xray panels reached over their HTTP APIs:
Marzban, Hiddify, Marzneshin and S-UI (Lifecycle/P6 design v7.1, sections
9.7 - 9.11).

The legacy add_client of every one of these clients OVERWRITES an existing
object of the same name when its first request fails (a PUT, a PATCH or a
full-row edit), and their list reads turn an error into "nothing there".
None of that is used here. Each adapter:

- reads with a strict method that either returns the real data or raises;
- treats "same name, different credential or inbound" as present_conflict
  and then changes and deletes nothing;
- creates with exactly one request and confirms with a second read;
- after a delete reads again, and accepts "no such object" answers only
  when they equal one of the node's verified matchers (adapter_base).

A 404 with no matching matcher is `unreadable`, never `absent`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .adapter_base import (
    AbsentOutcome,
    AdapterConflict,
    AdapterError,
    PresentResult,
    ReadResult,
    ReadState,
    http_matches,
)

MATCH, CONFLICT, ABSENT, UNREADABLE = (
    ReadState.PRESENT_MATCH, ReadState.PRESENT_CONFLICT, ReadState.ABSENT, ReadState.UNREADABLE,
)


@dataclass(frozen=True, repr=False)
class PanelIdentity:
    email: str                          # the connection's xr_email (panel "name"/"username"/"note")
    uuid: str                           # vless uuid / Hiddify uuid / Marzneshin key - a credential
    flow: str = ""
    inbound_tag: Optional[str] = None   # marzban
    inbound_id: Optional[int] = None    # marzneshin service id / s-ui inbound id
    username: Optional[str] = None      # marzneshin: sanitize_username(email), fixed when the step was created

    def __repr__(self) -> str:
        return (f"PanelIdentity(email={self.email!r}, inbound_tag={self.inbound_tag!r}, "
                f"inbound_id={self.inbound_id!r}, username={self.username!r})")


# ------------------------------------------------------------------ marzban
def read_marzban(xc, identity: PanelIdentity, matchers=()) -> ReadResult:
    try:
        status, body = xc.get_user_raw(identity.email)
    except Exception:  # noqa: BLE001
        return ReadResult(UNREADABLE)
    if status == 200 and isinstance(body, dict) and body.get("username") is not None:
        if body.get("username") != identity.email:
            # The panel answered for a differently-spelled name (it may
            # normalize usernames) - not provably ours.
            return ReadResult(CONFLICT, "conflict_username_normalized")
        vless = (body.get("proxies") or {}).get("vless") or {}
        if vless.get("id") != identity.uuid:
            return ReadResult(CONFLICT, "conflict_uuid_mismatch")
        if identity.inbound_tag not in ((body.get("inbounds") or {}).get("vless") or []):
            return ReadResult(CONFLICT, "conflict_inbound_mismatch")
        return ReadResult(MATCH, handle=body)
    if http_matches(matchers, "read", status, body):
        return ReadResult(ABSENT)
    return ReadResult(UNREADABLE)


def _delete_marzban(xc, identity, found, matchers):
    status, body = xc.delete_user_raw(identity.email)
    return http_matches(matchers, "delete", status, body)


# ------------------------------------------------------------------ hiddify
def read_hiddify(xc, identity: PanelIdentity, matchers=()) -> ReadResult:
    try:
        status, body = xc.list_users_raw()
    except Exception:  # noqa: BLE001
        return ReadResult(UNREADABLE)
    if status == 200 and isinstance(body, list):
        users = body
    elif http_matches(matchers, "list_empty", status, body):
        users = []      # this panel answers 404 for an empty list - only when verified for THIS node
    else:
        return ReadResult(UNREADABLE)
    same_uuid = [u for u in users if isinstance(u, dict) and u.get("uuid") == identity.uuid]
    same_name = [u for u in users if isinstance(u, dict) and u.get("name") == identity.email]
    if not same_uuid and not same_name:
        return ReadResult(ABSENT)
    if len(same_name) > 1:
        return ReadResult(CONFLICT, "conflict_duplicate_name")
    if same_uuid and same_uuid[0].get("name") != identity.email:
        return ReadResult(CONFLICT, "conflict_uuid_taken")
    if same_name and same_name[0].get("uuid") != identity.uuid:
        return ReadResult(CONFLICT, "conflict_name_taken")
    return ReadResult(MATCH, handle=same_uuid[0])


def _delete_hiddify(xc, identity, found, matchers):
    status, body = xc.delete_user_raw(identity.uuid)        # by uuid only - never "the first user with that name"
    return http_matches(matchers, "delete", status, body)


# --------------------------------------------------------------- marzneshin
def read_marzneshin(xc, identity: PanelIdentity, matchers=()) -> ReadResult:
    try:
        users = xc.list_users_strict()
    except Exception:  # noqa: BLE001
        return ReadResult(UNREADABLE)
    by_username = [u for u in users if isinstance(u, dict) and u.get("username") == identity.username]
    note_elsewhere = [u for u in users if isinstance(u, dict) and u.get("note") == identity.email
                      and u.get("username") != identity.username]
    if note_elsewhere:
        return ReadResult(CONFLICT, "conflict_note_under_other_username")
    if not by_username:
        return ReadResult(ABSENT)
    if len(by_username) > 1:
        return ReadResult(CONFLICT, "conflict_duplicate_username")
    user = by_username[0]
    if user.get("note") != identity.email:
        # sanitize_username is lossy: this is somebody else's user that
        # happens to collapse to the same panel username.
        return ReadResult(CONFLICT, "conflict_sanitized_username_collision")
    if "key" not in user:
        return ReadResult(UNREADABLE)      # the listing does not expose the key: identity cannot be proven
    if user.get("key") != identity.uuid:
        return ReadResult(CONFLICT, "conflict_key_mismatch")
    if identity.inbound_id not in (user.get("service_ids") or []):
        return ReadResult(CONFLICT, "conflict_service_mismatch")
    return ReadResult(MATCH, handle=user)


def _delete_marzneshin(xc, identity, found, matchers):
    status, body = xc.delete_user_raw(identity.username)
    return http_matches(matchers, "delete", status, body)


# --------------------------------------------------------------------- s-ui
def read_sui(xc, identity: PanelIdentity, matchers=()) -> ReadResult:
    try:
        records = xc.find_clients_strict(identity.email)
    except Exception:  # noqa: BLE001
        return ReadResult(UNREADABLE)
    if not records:
        return ReadResult(ABSENT)
    if len(records) > 1:
        return ReadResult(CONFLICT, "conflict_duplicate_name")
    record = records[0]
    config = record.get("config")
    vless = config.get("vless") if isinstance(config, dict) else None
    if not isinstance(vless, dict):
        return ReadResult(UNREADABLE)
    if vless.get("uuid") != identity.uuid:
        return ReadResult(CONFLICT, "conflict_uuid_mismatch")
    inbounds = record.get("inbounds")
    if not isinstance(inbounds, list) or identity.inbound_id not in inbounds:
        return ReadResult(CONFLICT, "conflict_inbound_mismatch")
    return ReadResult(MATCH, handle=record)


def _delete_sui(xc, identity, found, matchers):
    xc.delete_client_by_id(found.handle.get("id"))     # the id of the record THIS read matched
    return False                                         # s-ui has no "already gone" answer to verify


# ----------------------------------------------------------------- shared flow
def _present(prefix, read, create, is_enabled, enable):
    def ensure_present(xc, identity: PanelIdentity, matchers=()) -> PresentResult:
        found = read(xc, identity, matchers)
        if found.state is CONFLICT:
            raise AdapterConflict(found.conflict_code or "remote_identity_conflict")
        if found.state is UNREADABLE:
            raise AdapterError(f"{prefix}_unreadable")
        if found.state is MATCH:
            if not is_enabled(found.handle):
                # The only write ever made to an EXISTING object, and only
                # after a read proved it is exactly ours.
                try:
                    enable(xc, identity, found.handle)
                except Exception as exc:  # noqa: BLE001
                    raise AdapterError(f"{prefix}_enable_failed", type(exc).__name__) from None
            return PresentResult(created=False)
        try:
            create(xc, identity)
        except Exception as exc:  # noqa: BLE001
            raise AdapterError(f"{prefix}_create_failed", type(exc).__name__) from None
        if read(xc, identity, matchers).state is not MATCH:
            raise AdapterError(f"{prefix}_create_unconfirmed")
        return PresentResult(created=True)
    return ensure_present


def _absent(read, delete):
    def ensure_absent(xc, identity: PanelIdentity, matchers=()) -> AbsentOutcome:
        found = read(xc, identity, matchers)
        if found.state is ABSENT:
            return AbsentOutcome.VERIFIED_ABSENT
        if found.state is not MATCH:
            return AbsentOutcome.UNVERIFIED          # conflict or unreadable: delete nothing
        try:
            verified_not_exist = delete(xc, identity, found, matchers)
        except Exception:  # noqa: BLE001
            verified_not_exist = False
        after = read(xc, identity, matchers)
        if after.state is ABSENT:
            return AbsentOutcome.VERIFIED_ABSENT
        if after.state is UNREADABLE and verified_not_exist:
            return AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT
        return AbsentOutcome.UNVERIFIED
    return ensure_absent


ensure_present_marzban = _present(
    "marzban", read_marzban,
    lambda xc, i: xc.create_user_only(i.email, i.uuid, i.flow),
    lambda user: user.get("status") in (None, "active"),
    lambda xc, i, user: xc.enable_user_only(i.email),
)
ensure_absent_marzban = _absent(read_marzban, _delete_marzban)

ensure_present_hiddify = _present(
    "hiddify", read_hiddify,
    lambda xc, i: xc.create_user_only(i.email, i.uuid),
    lambda user: bool(user.get("enable", True)),
    lambda xc, i, user: xc.enable_user_only(i.uuid),
)
ensure_absent_hiddify = _absent(read_hiddify, _delete_hiddify)

ensure_present_marzneshin = _present(
    "marzneshin", read_marzneshin,
    lambda xc, i: xc.create_user_only(i.username, i.email, i.uuid),
    lambda user: bool(user.get("enabled", True)),
    lambda xc, i, user: xc.enable_user_only(i.username),
)
ensure_absent_marzneshin = _absent(read_marzneshin, _delete_marzneshin)

ensure_present_sui = _present(
    "sui", read_sui,
    lambda xc, i: xc.create_client_only(i.email, i.uuid, i.flow),
    lambda record: bool(record.get("enable", True)),
    lambda xc, i, record: xc.enable_client_full(record),
)
ensure_absent_sui = _absent(read_sui, _delete_sui)
