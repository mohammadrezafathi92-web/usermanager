"""Lifecycle/P6 batch L1a-3 - adapters for Xray over SSH, 3X-UI and
SoftEther (design v7.1, sections 9.3, 9.4, 9.5). Run:
    python3 backend/tests/test_adapter_xray_softether.py

Fake clients keep real state and a call log. Adapter-level coverage of
LP-41..LP-48 and LP-84..LP-88.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import adapter_softether as se, adapter_xray as ax
from app.services.adapter_base import AbsentOutcome, AdapterConflict, AdapterError, ReadState
from app.services.softether_client import SoftEtherClient, SoftEtherError
from app.services.threexui_client import ThreeXUIClient
from app.services.xray_client import XrayError

failures: list[str] = []
UUID_A, UUID_B = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"


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


class FakeXray:
    """Stands in for both XrayClient (ssh) and ThreeXUIClient: a client list
    for one inbound, plus counters for the expensive ssh side effects."""

    def __init__(self, clients=(), id_key="id"):
        self.clients = [dict(c) for c in clients]
        self.id_key = id_key
        self.log: list = []
        self.fail: dict = {}
        self.config_writes = self.restarts = 0
        self.add_is_noop = self.remove_is_noop = False

    def _call(self, name, *args):
        self.log.append((name,) + args)
        if name in self.fail:
            raise self.fail[name]

    def get_inbound_clients_strict(self, inbound_tag=None):
        self._call("get_inbound_clients_strict")
        return [dict(c) for c in self.clients]

    def add_client(self, inbound_tag, email, client_uuid=None, flow=""):
        self._call("add_client", email)
        if not self.add_is_noop:
            self.clients = [c for c in self.clients if c.get("email") != email] + [{self.id_key: client_uuid, "email": email, "flow": flow}]
        self.config_writes += 1
        self.restarts += 1
        return client_uuid

    def remove_client(self, inbound_tag, email, client_uuid=None):
        self._call("remove_client", email)
        if not self.remove_is_noop:
            self.clients = [c for c in self.clients if c.get("email") != email]
        self.config_writes += 1
        self.restarts += 1

    def writes(self):
        return [e[0] for e in self.log if e[0] in ("add_client", "remove_client")]


IDENT = ax.XrayIdentity(email="ali1a2b@usermanager.local", uuid=UUID_A, flow="xtls-rprx-vision", inbound_tag="vless-in")

for backend, read, present, absent in (
    ("xray_ssh", ax.read_ssh, ax.ensure_present_ssh, ax.ensure_absent_ssh),
    ("threexui", ax.read_threexui, ax.ensure_present_threexui, ax.ensure_absent_threexui),
):
    print(f"--- {backend} ---")
    check(f"[{backend}] no such email -> absent", read(FakeXray(), IDENT).state, ReadState.ABSENT)
    check(f"[{backend}] email and uuid equal -> present_match",
          read(FakeXray([{"id": UUID_A, "email": IDENT.email}]), IDENT).state, ReadState.PRESENT_MATCH)
    conflict = read(FakeXray([{"id": UUID_B, "email": IDENT.email}]), IDENT)
    check(f"[{backend}] LP-43: same email, another uuid -> present_conflict",
          (conflict.state, conflict.conflict_code), (ReadState.PRESENT_CONFLICT, "conflict_uuid_mismatch"))
    twice = read(FakeXray([{"id": UUID_A, "email": IDENT.email}, {"id": UUID_B, "email": IDENT.email}]), IDENT)
    check(f"[{backend}] the email present twice -> present_conflict", twice.conflict_code, "conflict_duplicate_email")
    down = FakeXray([{"id": UUID_A, "email": IDENT.email}])
    down.fail["get_inbound_clients_strict"] = XrayError("ssh down")
    check(f"[{backend}] cannot read -> unreadable (never absent)", read(down, IDENT).state, ReadState.UNREADABLE)

    fake = FakeXray()
    created = present(fake, IDENT)
    check(f"[{backend}] LP-41: absent -> created with the STAGED uuid (the client generates nothing)",
          (created.created, [(c["id"], c["email"]) for c in fake.clients]), (True, [(UUID_A, IDENT.email)]))
    writes_before = (fake.config_writes, fake.restarts)
    again = present(fake, IDENT)
    check(f"[{backend}] LP-42: a retry on present_match writes nothing and restarts nothing",
          (again.created, (fake.config_writes, fake.restarts), fake.writes()), (False, writes_before, ["add_client"]))

    other = FakeXray([{"id": UUID_B, "email": IDENT.email}])
    check(f"[{backend}] conflict -> AdapterConflict", raises_code(lambda: present(other, IDENT), AdapterConflict),
          "conflict_uuid_mismatch")
    check(f"[{backend}] ...and the other party's client is untouched", (other.writes(), other.clients),
          ([], [{"id": UUID_B, "email": IDENT.email}]))

    silent = FakeXray()
    silent.add_is_noop = True
    check(f"[{backend}] LP-44: add 'succeeds' but the client is not there on re-read -> not a success",
          raises_code(lambda: present(silent, IDENT)), f"{backend}_add_unconfirmed")
    unreadable = FakeXray()
    unreadable.fail["get_inbound_clients_strict"] = XrayError("timeout")
    check(f"[{backend}] unreadable -> retryable error and no write",
          (raises_code(lambda: present(unreadable, IDENT)), unreadable.writes()), (f"{backend}_unreadable", []))
    leaky = FakeXray()
    leaky.fail["add_client"] = XrayError(f"failed for uuid {UUID_A}")
    try:
        present(leaky, IDENT)
        text = ""
    except AdapterError as exc:
        text = f"{exc.code} {exc}"
    check(f"[{backend}] a client error that quotes the uuid is reduced to its type", (UUID_A in text, text),
          (False, f"{backend}_add_failed XrayError"))

    fake = FakeXray([{"id": UUID_A, "email": IDENT.email}, {"id": UUID_B, "email": "someone@else"}])
    check(f"[{backend}] ensure_absent: present -> removed, re-read, verified_absent; the neighbour stays",
          (absent(fake, IDENT), [c["email"] for c in fake.clients]), (AbsentOutcome.VERIFIED_ABSENT, ["someone@else"]))
    before = (fake.config_writes, fake.restarts)
    check(f"[{backend}] ensure_absent: already absent -> verified_absent with no write and no restart",
          (absent(fake, IDENT), (fake.config_writes, fake.restarts)), (AbsentOutcome.VERIFIED_ABSENT, before))
    stubborn = FakeXray([{"id": UUID_A, "email": IDENT.email}])
    stubborn.remove_is_noop = True
    check(f"[{backend}] LP-45: remove reports success but the client is still there -> unverified",
          absent(stubborn, IDENT), AbsentOutcome.UNVERIFIED)
    foreign = FakeXray([{"id": UUID_B, "email": IDENT.email}])
    check(f"[{backend}] ensure_absent on a conflict -> unverified, nothing deleted",
          (absent(foreign, IDENT), foreign.writes()), (AbsentOutcome.UNVERIFIED, []))
    dead = FakeXray([{"id": UUID_A, "email": IDENT.email}])
    dead.fail["get_inbound_clients_strict"] = XrayError("down")
    check(f"[{backend}] ensure_absent unreadable -> unverified, nothing deleted",
          (absent(dead, IDENT), dead.writes()), (AbsentOutcome.UNVERIFIED, []))

check("xray_ssh: a trojan entry (uuid stored as 'password') matches",
      ax.read_ssh(FakeXray([{"password": UUID_A, "email": IDENT.email}]), IDENT).state, ReadState.PRESENT_MATCH)
check("the identity's repr/str never shows the uuid", UUID_A in f"{IDENT!r} {IDENT}", False)

print("--- 3X-UI strict read on the real client class (no network) ---")


def threexui_with(obj):
    client = ThreeXUIClient.__new__(ThreeXUIClient)
    client._get_inbound = lambda: obj
    return client


check("a normal inbound -> its client list",
      threexui_with({"settings": '{"clients": [{"id": "x", "email": "e"}]}'}).get_inbound_clients_strict(),
      [{"id": "x", "email": "e"}])
check("an inbound with no clients -> an empty list (that IS readable)",
      threexui_with({"settings": '{"clients": []}'}).get_inbound_clients_strict(), [])
for label, obj in (("inbound missing ({} from the panel)", {}), ("no settings key", {"id": 3}),
                   ("settings is not JSON", {"settings": "<html>"}), ("settings has no client list", {"settings": "{}"}),
                   ("settings is null", {"settings": None})):
    try:
        threexui_with(obj).get_inbound_clients_strict()
        outcome = "returned a list"
    except XrayError:
        outcome = "XrayError"
    check(f"{label} -> raises instead of looking like 'no clients'", outcome, "XrayError")

print("--- softether ---")


class FakeHub:
    def __init__(self, users=()):
        self.users = set(users)
        self.passwords: dict = {}
        self.log: list = []
        self.enum_fails = None       # exception to raise from user_exists
        self.delete_result = None    # force delete_user_typed's answer
        self.delete_is_noop = False

    def user_exists(self, username):
        self.log.append(("user_exists", username))
        if self.enum_fails:
            raise self.enum_fails
        return username in self.users

    def add_client(self, username, password):
        self.log.append(("add_client", username))
        self.users.add(username)
        self.passwords[username] = password

    def set_client_enabled(self, username, password, enabled):
        self.log.append(("set_client_enabled", username, enabled))
        self.passwords[username] = password
        return True

    def delete_user_typed(self, username, not_exist_codes=()):
        self.log.append(("delete_user_typed", username, tuple(not_exist_codes)))
        if self.delete_result:
            return self.delete_result
        if username not in self.users:
            return "ambiguous"
        if not self.delete_is_noop:
            self.users.discard(username)
        return "deleted"


WHO = se.SoftEtherIdentity(username="ali1a2b", password="staged-pw")
hub = FakeHub()
check("absent -> created with the staged password",
      (se.ensure_present(hub, WHO).created, hub.passwords), (True, {"ali1a2b": "staged-pw"}))
hub = FakeHub(users=["ali1a2b"])
hub.passwords["ali1a2b"] = "unknown-old"
check("LP-47: already there (retry after an ambiguous create) -> SetUser with the staged password, still one user",
      (se.ensure_present(hub, WHO).created, hub.passwords, [e[0] for e in hub.log]),
      (False, {"ali1a2b": "staged-pw"}, ["user_exists", "set_client_enabled"]))
hub = FakeHub()
hub.enum_fails = SoftEtherError("timeout")
check("cannot list users -> retryable error, nothing created",
      (raises_code(lambda: se.ensure_present(hub, WHO)), hub.users), ("softether_unreadable", set()))
check("the identity's repr never shows the password", "staged-pw" in f"{WHO!r} {WHO}", False)

hub = FakeHub(users=["ali1a2b", "other"])
check("LP-86: deleted and the listing no longer has the user -> verified_absent",
      (se.ensure_absent(hub, WHO), hub.users), (AbsentOutcome.VERIFIED_ABSENT, {"other"}))
hub = FakeHub(users=["other"])
check("LP-86: the user was never there, listing works -> verified_absent", se.ensure_absent(hub, WHO), AbsentOutcome.VERIFIED_ABSENT)
hub = FakeHub(users=["ali1a2b"])
hub.enum_fails = SoftEtherError("timeout")
check("LP-84: DeleteUser fine, EnumUser times out -> unverified (a clean delete alone proves nothing)",
      se.ensure_absent(hub, WHO), AbsentOutcome.UNVERIFIED)
hub = FakeHub(users=["ali1a2b"])
hub.enum_fails = SoftEtherError("HTML / 403 instead of JSON")
check("LP-85: DeleteUser fine, EnumUser answers HTML/403 -> unverified", se.ensure_absent(hub, WHO), AbsentOutcome.UNVERIFIED)
hub = FakeHub()
hub.delete_result, hub.enum_fails = "not_exist", SoftEtherError("down")
check("LP-87: a verified 'no such user' code and no listing -> delete_idempotently_absent",
      se.ensure_absent(hub, WHO, not_exist_codes=(29,)), AbsentOutcome.DELETE_IDEMPOTENTLY_ABSENT)
hub = FakeHub()
hub.delete_result, hub.enum_fails = "ambiguous", SoftEtherError("down")
check("LP-88: an ambiguous delete and no listing -> unverified", se.ensure_absent(hub, WHO), AbsentOutcome.UNVERIFIED)
hub = FakeHub(users=["ali1a2b"])
hub.delete_is_noop = True
check("delete 'succeeds' but the listing still has the user -> unverified", se.ensure_absent(hub, WHO), AbsentOutcome.UNVERIFIED)

print("--- softether typed methods on the real client class (no network) ---")


def hub_client(call):
    client = SoftEtherClient.__new__(SoftEtherClient)
    client.hub_name = "VPN"
    client._call = call
    return client


def rpc_error(code):
    exc = SoftEtherError(f"error {code}: object does not exist")
    exc.rpc_code = code
    return exc


def raising(exc):
    def call(method, params):
        raise exc
    return call


users = {"UserList": [{"Name_str": "fresh", "IsTrafficFilled_bool": False}]}
check("LP-46: user_exists sees a user that has no traffic yet", hub_client(lambda m, p: users).user_exists("fresh"), True)
check("user_exists: an empty hub is a valid answer", hub_client(lambda m, p: {"UserList": []}).user_exists("x"), False)
for label, payload in (("no UserList key", {}), ("UserList is not a list", {"UserList": "oops"})):
    try:
        hub_client(lambda m, p, _p=payload: _p).user_exists("x")
        outcome = "returned"
    except SoftEtherError:
        outcome = "SoftEtherError"
    check(f"user_exists: {label} -> raises (not 'no such user')", outcome, "SoftEtherError")
check("delete_user_typed: a clean answer -> deleted", hub_client(lambda m, p: {}).delete_user_typed("u"), "deleted")
check("LP-87: an error with a verified code -> not_exist",
      hub_client(raising(rpc_error(29))).delete_user_typed("u", not_exist_codes=(29,)), "not_exist")
check("LP-87: the same error with an EMPTY verified list -> ambiguous",
      hub_client(raising(rpc_error(29))).delete_user_typed("u"), "ambiguous")
check("LP-88: an unknown code whose TEXT says 'does not exist' -> ambiguous (text is not trusted)",
      hub_client(raising(rpc_error(77))).delete_user_typed("u", not_exist_codes=(29,)), "ambiguous")
check("LP-88: a connection failure (no rpc code at all) -> ambiguous",
      hub_client(raising(SoftEtherError("connection refused - not exist"))).delete_user_typed("u", not_exist_codes=(29,)),
      "ambiguous")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
