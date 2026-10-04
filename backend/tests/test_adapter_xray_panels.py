"""Lifecycle/P6 batch L1a-4 - adapters for Marzban, Hiddify, Marzneshin and
S-UI (design v7.1, sections 9.7 - 9.11), and the not-exist matchers.
Run:  python3 backend/tests/test_adapter_xray_panels.py

One fake panel per backend keeps real state and a log of every WRITE, so
each scenario asserts both the outcome and what was (not) sent.
Covers LP-111 .. LP-124 and the matcher half of LP-174.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import adapter_base as ab, adapter_xray_panels as ap
from app.services.adapter_base import AbsentOutcome, AdapterConflict, AdapterError, ReadState
from app.services.marzneshin_client import MarzneshinClient, sanitize_username
from app.services.sui_client import SuiClient
from app.services.xray_client import XrayError

failures: list[str] = []
UUID_A, UUID_B = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"
EMAIL = "ali1a2b@usermanager.local"


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def raises_code(fn, exc_type=AdapterError):
    try:
        fn()
    except exc_type as exc:
        return exc.code
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__}"
    return None


class Fake:
    def __init__(self):
        self.writes: list = []
        self.read_fails = False
        self.create_is_noop = False
        self.delete_is_noop = False
        self.delete_answer = None        # (status, body) to return instead of acting


# ------------------------------------------------------------------ marzban
class FakeMarzban(Fake):
    NOT_FOUND = (404, {"detail": "User not found"})

    def __init__(self, users=()):
        super().__init__()
        self.users = {u["username"]: dict(u) for u in users}

    def get_user_raw(self, username):
        if self.read_fails:
            raise XrayError("panel down")
        return (200, dict(self.users[username])) if username in self.users else self.NOT_FOUND

    def create_user_only(self, username, client_uuid, flow=""):
        self.writes.append(("POST", username))
        if not self.create_is_noop:
            self.users[username] = {"username": username, "status": "active",
                                    "proxies": {"vless": {"id": client_uuid, "flow": flow}},
                                    "inbounds": {"vless": ["vless-in"]}}

    def enable_user_only(self, username):
        self.writes.append(("PUT-status", username))
        self.users[username]["status"] = "active"

    def delete_user_raw(self, username):
        self.writes.append(("DELETE", username))
        if self.delete_answer:
            return self.delete_answer
        if username not in self.users:
            return self.NOT_FOUND
        if not self.delete_is_noop:
            del self.users[username]
        return 200, {}


def marzban_user(**over):
    user = {"username": EMAIL, "status": "active", "proxies": {"vless": {"id": UUID_A, "flow": ""}},
            "inbounds": {"vless": ["vless-in"]}}
    user.update(over)
    return user


MZ = ap.PanelIdentity(email=EMAIL, uuid=UUID_A, inbound_tag="vless-in")
MZ_MATCHERS = [{"kind": "http", "op": "read", "status": 404, "json_path": "detail", "equals": "User not found"},
               {"kind": "http", "op": "delete", "status": 404, "json_path": "detail", "equals": "User not found"}]

print("--- marzban ---")
check("a 404 with NO verified matcher is unreadable, not absent", ap.read_marzban(FakeMarzban(), MZ).state, ReadState.UNREADABLE)
check("the same 404 with this node's verified matcher is absent", ap.read_marzban(FakeMarzban(), MZ, MZ_MATCHERS).state, ReadState.ABSENT)
check("a 404 whose body differs from the matcher stays unreadable",
      ap.read_marzban(type("P", (FakeMarzban,), {"NOT_FOUND": (404, {"detail": "Not Found"})})(), MZ, MZ_MATCHERS).state,
      ReadState.UNREADABLE)
check("username, uuid and inbound equal -> present_match", ap.read_marzban(FakeMarzban([marzban_user()]), MZ, MZ_MATCHERS).state,
      ReadState.PRESENT_MATCH)
for label, user, code in (
    ("LP-115: same username, another uuid", marzban_user(proxies={"vless": {"id": UUID_B}}), "conflict_uuid_mismatch"),
    ("LP-115: same username, another inbound tag", marzban_user(inbounds={"vless": ["other-in"]}), "conflict_inbound_mismatch"),
):
    panel = FakeMarzban([user])
    result = ap.read_marzban(panel, MZ, MZ_MATCHERS)
    check(f"{label} -> present_conflict", (result.state, result.conflict_code), (ReadState.PRESENT_CONFLICT, code))
    check(f"{label}: ensure_present raises and sends NOTHING",
          (raises_code(lambda: ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS), AdapterConflict), panel.writes), (code, []))
    check(f"{label}: ensure_absent is unverified and deletes NOTHING",
          (ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), panel.writes), (AbsentOutcome.UNVERIFIED, []))
normalized = FakeMarzban()
normalized.get_user_raw = lambda username: (200, marzban_user(username=EMAIL.upper()))
check("the panel answering under a differently-spelled username is a conflict, not a match",
      ap.read_marzban(normalized, MZ, MZ_MATCHERS).conflict_code, "conflict_username_normalized")

panel = FakeMarzban()
check("LP-111: absent -> exactly one POST, never a PUT",
      (ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS).created, panel.writes), (True, [("POST", EMAIL)]))
check("LP-111: a retry (the user is there now) sends nothing more",
      (ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS).created, panel.writes), (False, [("POST", EMAIL)]))
panel = FakeMarzban([marzban_user(status="disabled")])
ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS)
check("a matching but disabled user is only re-enabled (a status-only PUT)", panel.writes, [("PUT-status", EMAIL)])
panel = FakeMarzban()
panel.create_is_noop = True
check("create that is not there on re-read is not a success",
      raises_code(lambda: ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS)), "marzban_create_unconfirmed")
panel = FakeMarzban()
panel.read_fails = True
check("unreadable -> retryable error, nothing sent",
      (raises_code(lambda: ap.ensure_present_marzban(panel, MZ, MZ_MATCHERS)), panel.writes), ("marzban_unreadable", []))

panel = FakeMarzban([marzban_user()])
check("LP-119: present -> DELETE, re-read absent -> verified_absent",
      (ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), panel.writes), (AbsentOutcome.VERIFIED_ABSENT, [("DELETE", EMAIL)]))
check("LP-119: already absent -> verified_absent, no DELETE",
      (ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), len(panel.writes)), (AbsentOutcome.VERIFIED_ABSENT, 1))
panel = FakeMarzban([marzban_user()])
panel.delete_is_noop, panel.delete_answer = True, (500, {"detail": "boom"})
check("LP-119: an ambiguous DELETE and the user still listed -> unverified",
      ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), AbsentOutcome.UNVERIFIED)
panel = FakeMarzban([marzban_user()])
panel.delete_answer = FakeMarzban.NOT_FOUND


def _delete_then_down(username, _p=panel):
    _p.writes.append(("DELETE", username))
    _p.read_fails = True
    return FakeMarzban.NOT_FOUND


panel.delete_user_raw = _delete_then_down
check("a verified 'not found' from DELETE and an unreadable re-read -> delete_idempotently_absent",
      ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT)
panel = FakeMarzban([marzban_user()])
panel.delete_user_raw = lambda username, _p=panel: (_p.writes.append(("DELETE", username)), setattr(_p, "read_fails", True), (404, {}))[2]
check("the same with an UNverified 404 body -> unverified",
      ap.ensure_absent_marzban(panel, MZ, MZ_MATCHERS), AbsentOutcome.UNVERIFIED)


# ------------------------------------------------------------------ hiddify
class FakeHiddify(Fake):
    def __init__(self, users=()):
        super().__init__()
        self.users = [dict(u) for u in users]
        self.list_answer = None

    def list_users_raw(self):
        if self.read_fails:
            raise XrayError("down")
        return self.list_answer or (200, [dict(u) for u in self.users])

    def create_user_only(self, name, client_uuid):
        self.writes.append(("POST", name))
        if not self.create_is_noop:
            self.users.append({"uuid": client_uuid, "name": name, "enable": True})

    def enable_user_only(self, client_uuid):
        self.writes.append(("PATCH-enable", client_uuid))

    def delete_user_raw(self, client_uuid):
        self.writes.append(("DELETE", client_uuid))
        if not self.delete_is_noop:
            self.users = [u for u in self.users if u["uuid"] != client_uuid]
        return 200, {}


HD = ap.PanelIdentity(email=EMAIL, uuid=UUID_A)
print("--- hiddify ---")
check("no user with that uuid or name -> absent", ap.read_hiddify(FakeHiddify(), HD).state, ReadState.ABSENT)
check("uuid and name on the same row -> present_match",
      ap.read_hiddify(FakeHiddify([{"uuid": UUID_A, "name": EMAIL, "enable": True}]), HD).state, ReadState.PRESENT_MATCH)
for label, users, code in (
    ("LP-116: same name, another uuid", [{"uuid": UUID_B, "name": EMAIL}], "conflict_name_taken"),
    ("LP-116: same uuid, another name", [{"uuid": UUID_A, "name": "someone"}], "conflict_uuid_taken"),
    ("LP-116: two users with that name", [{"uuid": UUID_A, "name": EMAIL}, {"uuid": UUID_B, "name": EMAIL}], "conflict_duplicate_name"),
):
    panel = FakeHiddify(users)
    result = ap.read_hiddify(panel, HD)
    check(f"{label} -> present_conflict ({code})", (result.state, result.conflict_code), (ReadState.PRESENT_CONFLICT, code))
    check(f"{label}: no POST, PATCH or DELETE from either operation",
          (raises_code(lambda: ap.ensure_present_hiddify(panel, HD), AdapterConflict),
           ap.ensure_absent_hiddify(panel, HD), panel.writes), (code, AbsentOutcome.UNVERIFIED, []))
panel = FakeHiddify([{"uuid": UUID_A, "name": EMAIL}])
panel.list_answer = (404, {"message": "Not Found"})
check("LP-120: a 404 on the user list without a verified matcher is unreadable, not 'no users'",
      ap.read_hiddify(panel, HD).state, ReadState.UNREADABLE)
empty_matcher = [{"kind": "http", "op": "list_empty", "status": 404, "json_path": "message", "equals": "You have no user"}]
panel.list_answer = (404, {"message": "You have no user"})
check("...and with this node's verified 'empty list' matcher it is an empty list",
      ap.read_hiddify(panel, HD, empty_matcher).state, ReadState.ABSENT)
panel.list_answer = (200, {"not": "a list"})
check("a 200 that is not a list is unreadable", ap.read_hiddify(panel, HD).state, ReadState.UNREADABLE)
panel = FakeHiddify()
check("LP-112: absent -> one POST; a retry sends nothing; never a full-body PATCH",
      (ap.ensure_present_hiddify(panel, HD).created, ap.ensure_present_hiddify(panel, HD).created, panel.writes),
      (True, False, [("POST", EMAIL)]))
panel = FakeHiddify([{"uuid": UUID_A, "name": EMAIL, "enable": False}])
ap.ensure_present_hiddify(panel, HD)
check("a matching but disabled user gets only the enable PATCH", panel.writes, [("PATCH-enable", UUID_A)])
panel = FakeHiddify([{"uuid": UUID_A, "name": EMAIL}, {"uuid": UUID_B, "name": "neighbour"}])
check("LP-120: delete by uuid, re-read -> verified_absent; the neighbour stays",
      (ap.ensure_absent_hiddify(panel, HD), [u["name"] for u in panel.users]), (AbsentOutcome.VERIFIED_ABSENT, ["neighbour"]))
panel = FakeHiddify([{"uuid": UUID_A, "name": EMAIL}])
panel.delete_is_noop = True
check("LP-120: delete reported but the user is still there -> unverified",
      ap.ensure_absent_hiddify(panel, HD), AbsentOutcome.UNVERIFIED)


# --------------------------------------------------------------- marzneshin
class FakeMarzneshin(Fake):
    def __init__(self, users=()):
        super().__init__()
        self.users = [dict(u) for u in users]

    def list_users_strict(self):
        if self.read_fails:
            raise XrayError("page 2 was not a 200")
        return [dict(u) for u in self.users]

    def create_user_only(self, username, email, client_uuid):
        self.writes.append(("POST", username))
        if not self.create_is_noop:
            self.users.append({"username": username, "note": email, "key": client_uuid, "service_ids": [7], "enabled": True})

    def enable_user_only(self, username):
        self.writes.append(("POST-enable", username))

    def delete_user_raw(self, username):
        self.writes.append(("DELETE", username))
        if not self.delete_is_noop:
            self.users = [u for u in self.users if u["username"] != username]
        return 200, {}


USERNAME = sanitize_username(EMAIL)
MN = ap.PanelIdentity(email=EMAIL, uuid=UUID_A, inbound_id=7, username=USERNAME)


def mn_user(**over):
    user = {"username": USERNAME, "note": EMAIL, "key": UUID_A, "service_ids": [7], "enabled": True}
    user.update(over)
    return user


print("--- marzneshin ---")
check("the panel username is the sanitized email", USERNAME, "ali1a2busermanagerlocal")
check("absent", ap.read_marzneshin(FakeMarzneshin(), MN).state, ReadState.ABSENT)
check("username, note, key and service all equal -> present_match",
      ap.read_marzneshin(FakeMarzneshin([mn_user()]), MN).state, ReadState.PRESENT_MATCH)
for label, users, code in (
    ("LP-123: same sanitized username, another note (someone else's user)", [mn_user(note="ali.1a2b@usermanager.local")],
     "conflict_sanitized_username_collision"),
    ("LP-117: same username, another key", [mn_user(key=UUID_B)], "conflict_key_mismatch"),
    ("LP-117: same username, another service", [mn_user(service_ids=[9])], "conflict_service_mismatch"),
    ("LP-117: our note under a different username", [mn_user(username="somethingelse")], "conflict_note_under_other_username"),
):
    panel = FakeMarzneshin(users)
    result = ap.read_marzneshin(panel, MN)
    check(f"{label} -> present_conflict ({code})", (result.state, result.conflict_code), (ReadState.PRESENT_CONFLICT, code))
    check(f"{label}: no POST, no PUT, no DELETE - the existing user is not overwritten",
          (raises_code(lambda: ap.ensure_present_marzneshin(panel, MN), AdapterConflict),
           ap.ensure_absent_marzneshin(panel, MN), panel.writes), (code, AbsentOutcome.UNVERIFIED, []))
check("a listing that does not expose the key cannot prove identity -> unreadable",
      ap.read_marzneshin(FakeMarzneshin([{k: v for k, v in mn_user().items() if k != "key"}]), MN).state, ReadState.UNREADABLE)
panel = FakeMarzneshin([mn_user()])
panel.read_fails = True
check("LP-121: an incomplete listing is unreadable, not absent", ap.read_marzneshin(panel, MN).state, ReadState.UNREADABLE)
panel = FakeMarzneshin()
check("LP-113: absent -> one POST with the sanitized username; a retry sends nothing",
      (ap.ensure_present_marzneshin(panel, MN).created, ap.ensure_present_marzneshin(panel, MN).created, panel.writes),
      (True, False, [("POST", USERNAME)]))
panel = FakeMarzneshin([mn_user()])
check("LP-121: delete, re-read -> verified_absent", ap.ensure_absent_marzneshin(panel, MN), AbsentOutcome.VERIFIED_ABSENT)
panel = FakeMarzneshin([mn_user()])
panel.delete_is_noop = True
check("LP-121: delete reported, user still listed -> unverified", ap.ensure_absent_marzneshin(panel, MN), AbsentOutcome.UNVERIFIED)


def marzneshin_pages(pages):
    client = MarzneshinClient.__new__(MarzneshinClient)
    calls = iter(pages)
    client._get = lambda path: next(calls)
    return client


check("list_users_strict on the real client: a full single page",
      marzneshin_pages([(200, {"items": [{"username": "a"}], "total": 1, "pages": 1})]).list_users_strict(), [{"username": "a"}])
for label, pages in (
    ("a non-200 first page", [(500, {})]),
    ("a non-200 SECOND page", [(200, {"items": [{"username": "a"}] * 2, "total": 4, "pages": 2}), (502, {})]),
    ("items missing", [(200, {"total": 0})]),
    ("total disagreeing with what was read", [(200, {"items": [{"username": "a"}], "total": 5, "pages": 1})]),
):
    try:
        marzneshin_pages(pages).list_users_strict(page_size=2)
        outcome = "returned"
    except XrayError:
        outcome = "XrayError"
    check(f"LP-121: {label} -> raises instead of a partial list", outcome, "XrayError")


# --------------------------------------------------------------------- s-ui
class FakeSui(Fake):
    def __init__(self, clients=()):
        super().__init__()
        self.clients = [dict(c) for c in clients]
        self._id = 50

    def find_clients_strict(self, name):
        if self.read_fails:
            raise XrayError("success: false")
        return [dict(c) for c in self.clients if c["name"] == name]

    def create_client_only(self, name, client_uuid, flow=""):
        self.writes.append(("new", name))
        if not self.create_is_noop:
            self._id += 1
            self.clients.append({"id": self._id, "name": name, "enable": True,
                                 "config": {"vless": {"uuid": client_uuid, "flow": flow}}, "inbounds": [3]})

    def enable_client_full(self, record):
        self.writes.append(("edit", record["id"], sorted(record)))

    def delete_client_by_id(self, client_id):
        self.writes.append(("del", client_id))
        if not self.delete_is_noop:
            self.clients = [c for c in self.clients if c["id"] != client_id]


def sui_client(**over):
    client = {"id": 41, "name": EMAIL, "enable": True, "config": {"vless": {"uuid": UUID_A, "flow": ""}}, "inbounds": [3]}
    client.update(over)
    return client


SU = ap.PanelIdentity(email=EMAIL, uuid=UUID_A, inbound_id=3)
print("--- s-ui ---")
check("absent", ap.read_sui(FakeSui(), SU).state, ReadState.ABSENT)
check("name, uuid inside config.vless and inbound equal -> present_match", ap.read_sui(FakeSui([sui_client()]), SU).state,
      ReadState.PRESENT_MATCH)
for label, clients, code in (
    ("LP-124: same name, another uuid", [sui_client(config={"vless": {"uuid": UUID_B}})], "conflict_uuid_mismatch"),
    ("LP-124 / LP-118: same name and uuid, another inbound", [sui_client(inbounds=[9])], "conflict_inbound_mismatch"),
    ("LP-118: two clients with that name", [sui_client(), sui_client(id=42)], "conflict_duplicate_name"),
):
    panel = FakeSui(clients)
    result = ap.read_sui(panel, SU)
    check(f"{label} -> present_conflict ({code})", (result.state, result.conflict_code), (ReadState.PRESENT_CONFLICT, code))
    check(f"{label}: no new, edit or del", (raises_code(lambda: ap.ensure_present_sui(panel, SU), AdapterConflict),
                                           ap.ensure_absent_sui(panel, SU), panel.writes), (code, AbsentOutcome.UNVERIFIED, []))
check("a record without config.vless cannot prove identity -> unreadable",
      ap.read_sui(FakeSui([sui_client(config={})]), SU).state, ReadState.UNREADABLE)
panel = FakeSui()
check("LP-114: absent -> one 'new'; a retry sends no 'edit'",
      (ap.ensure_present_sui(panel, SU).created, ap.ensure_present_sui(panel, SU).created, panel.writes),
      (True, False, [("new", EMAIL)]))
panel = FakeSui([sui_client(enable=False, volume=5)])
ap.ensure_present_sui(panel, SU)
check("a matching but disabled client is edited with the COMPLETE record just read",
      panel.writes, [("edit", 41, ["config", "enable", "id", "inbounds", "name", "volume"])])
panel = FakeSui([sui_client(), sui_client(id=77, name="neighbour")])
check("LP-122: del with the id of the matched record, re-read -> verified_absent",
      (ap.ensure_absent_sui(panel, SU), panel.writes, [c["name"] for c in panel.clients]),
      (AbsentOutcome.VERIFIED_ABSENT, [("del", 41)], ["neighbour"]))
panel = FakeSui([sui_client()])
panel.delete_is_noop = True
check("LP-122: del reported, client still there -> unverified", ap.ensure_absent_sui(panel, SU), AbsentOutcome.UNVERIFIED)
panel = FakeSui([sui_client()])
panel.delete_client_by_id = lambda client_id, _p=panel: (_p.writes.append(("del", client_id)), setattr(_p, "read_fails", True))
check("s-ui never yields delete_idempotently_absent (it has no verifiable 'already gone' answer)",
      ap.ensure_absent_sui(panel, SU), AbsentOutcome.UNVERIFIED)


def sui_with(list_obj, full=None):
    client = SuiClient.__new__(SuiClient)
    client._get = lambda action, params=None: (full if params else list_obj)
    return client


check("find_clients_strict on the real client returns FULL records",
      sui_with({"clients": [{"id": 1, "name": EMAIL}]}, {"clients": [sui_client(id=1)]}).find_clients_strict(EMAIL),
      [sui_client(id=1)])
for label, obj in (("no client list in the answer", {}), ("a null answer", None), ("clients not a list", {"clients": "x"})):
    try:
        sui_with(obj).find_clients_strict(EMAIL)
        outcome = "returned"
    except XrayError:
        outcome = "XrayError"
    check(f"find_clients_strict: {label} -> raises, never 'no clients'", outcome, "XrayError")

print("--- the uuid never shows ---")
check("identity repr/str", UUID_A in f"{MZ!r} {HD} {MN!r} {SU}", False)
leaky = FakeMarzban()
leaky.create_user_only = lambda *a, **k: (_ for _ in ()).throw(XrayError(f"rejected uuid {UUID_A}"))
try:
    ap.ensure_present_marzban(leaky, MZ, MZ_MATCHERS)
    text = ""
except AdapterError as exc:
    text = f"{exc.code} {exc}"
check("a panel error quoting the uuid is reduced to its type", text, "marzban_create_failed XrayError")

print("--- not-exist matchers: a closed, exact schema (LP-174) ---")
good_http = {"kind": "http", "op": "read", "status": 404, "json_path": "detail", "equals": "User not found"}
good_rpc = {"kind": "jsonrpc", "op": "delete", "error_code": 29}
check("well-formed matchers are accepted", ab.validate_matchers([good_http, good_rpc]), [good_http, good_rpc])
bad = {
    "a status-only http matcher ('any 404')": {"kind": "http", "op": "read", "status": 404},
    "a status range": {**good_http, "status": "4xx"},
    "a regex/contains key": {**good_http, "contains": "not"},
    "a wildcard path": {**good_http, "json_path": "*"},
    "a path with an index/expression": {**good_http, "json_path": "detail[0]"},
    "an empty path": {**good_http, "json_path": ""},
    "a non-scalar equals": {**good_http, "equals": {"a": 1}},
    "a null equals": {**good_http, "equals": None},
    "an unknown kind": {"kind": "exception", "op": "read"},
    "an unknown op": {**good_http, "op": "any"},
    "list_empty for json-rpc": {**good_rpc, "op": "list_empty"},
    "a non-integer rpc code": {**good_rpc, "error_code": "29"},
    "an extra unknown key": {**good_rpc, "message": "x"},
    "not an object": "404",
}
for label, matcher in bad.items():
    try:
        ab.validate_matchers([matcher])
        outcome = "accepted"
    except ab.InvalidMatcher:
        outcome = "rejected"
    check(f"{label} is rejected", outcome, "rejected")
try:
    ab.validate_matchers([dict(good_http, equals=f"v{i}") for i in range(5)])
    outcome = "accepted"
except ab.InvalidMatcher:
    outcome = "rejected"
check("a fifth matcher for the same op is rejected", outcome, "rejected")
check("exact status + exact body value matches", ab.http_matches([good_http], "read", 404, {"detail": "User not found"}), True)
for label, args in (
    ("another status", ("read", 500, {"detail": "User not found"})),
    ("another body value", ("read", 404, {"detail": "Not Found"})),
    ("a body without the key", ("read", 404, {})),
    ("a non-JSON (None) body", ("read", 404, None)),
    ("another op", ("delete", 404, {"detail": "User not found"})),
    ("a value of another type (1 vs '1')", ("read", 404, {"detail": 1})),
):
    check(f"{label} does not match", ab.http_matches([good_http], *args), False)
check("an invalid matcher list matches nothing", ab.http_matches([{"kind": "http", "op": "read", "status": 404}], "read", 404, {}), False)
check("an empty matcher list matches nothing", ab.http_matches([], "read", 404, {"detail": "User not found"}), False)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
