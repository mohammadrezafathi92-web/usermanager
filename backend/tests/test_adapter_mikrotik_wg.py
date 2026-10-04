"""Lifecycle/P6 batch L1a-3 - the WireGuard/MikroTik adapter and the
interface-address contract (design v7.1, section 9.1). Run:
    python3 backend/tests/test_adapter_mikrotik_wg.py

A fake router keeps real state (peers, interface addresses, queues) and a
log of every call, so each scenario can assert both the outcome and that
nothing else was touched. Adapter-level coverage of LP-38, LP-39, LP-40,
LP-57, LP-79, LP-80, LP-81, LP-83.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import adapter_mikrotik_wg as wg, adapter_radius_ppp as ppp, user_ops
from app.services.adapter_base import AbsentOutcome, AdapterConflict, AdapterError, AdapterPermanentError, ReadState

failures: list[str] = []


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


class FakeRouter:
    WRITERS = {"ensure_wireguard_interface", "ensure_interface_address", "set_interface_address",
               "add_peer", "remove_peer", "upsert_simple_queue", "remove_simple_queue"}

    def __init__(self, addresses=(), peers=()):
        self.addresses = list(addresses)      # ["10.66.66.1/24"]
        self.peers = [dict(p) for p in peers]
        self.queues: dict = {}
        self.log: list = []
        self.fail: dict = {}                  # method -> exception to raise
        self._next_id = 100

    def _call(self, name, *args):
        self.log.append((name,) + args)
        if name in self.fail:
            exc = self.fail[name]
            if callable(exc):
                exc = exc()
            if exc:
                raise exc

    def writes(self):
        return [entry[0] for entry in self.log if entry[0] in self.WRITERS]

    # reads
    def list_peers(self, interface=None):
        self._call("list_peers", interface)
        return [dict(p) for p in self.peers if p.get("interface") == interface]

    def list_interface_addresses(self, interface):
        self._call("list_interface_addresses", interface)
        return [{".id": f"*a{i}", "address": a, "interface": interface} for i, a in enumerate(self.addresses)]

    # writes
    def ensure_wireguard_interface(self, name, listen_port=13231):
        self._call("ensure_wireguard_interface", name, listen_port)

    def ensure_interface_address(self, interface, address):
        self._call("ensure_interface_address", interface, address)
        if not self.addresses:                      # real method: only adds when there is none
            self.addresses.append(address)

    def set_interface_address(self, interface, address):
        self._call("set_interface_address", interface, address)
        self.addresses = [address]

    def add_peer(self, interface, public_key, allowed_address, comment):
        self._call("add_peer", interface, public_key, allowed_address, comment)
        self._next_id += 1
        self.peers.append({".id": f"*{self._next_id}", "interface": interface, "public-key": public_key,
                           "allowed-address": allowed_address, "comment": comment})
        return f"*{self._next_id}"

    def remove_peer(self, peer_id):
        self._call("remove_peer", peer_id)
        self.peers = [p for p in self.peers if p[".id"] != peer_id]

    def upsert_simple_queue(self, name, target, limit_mbps, disabled=False):
        self._call("upsert_simple_queue", name, target, limit_mbps)
        self.queues[name] = (target, limit_mbps)

    def remove_simple_queue(self, name):
        self._call("remove_simple_queue", name)
        self.queues.pop(name, None)


IDENT = wg.WireguardIdentity(interface="wg0", peer_name="user-ali-abc123", public_key="PUBKEY-A",
                             client_address="10.66.66.2/32")
SUBNET = "10.66.66.0/24"


def peer(**over):
    row = {".id": "*7", "interface": "wg0", "public-key": IDENT.public_key,
           "allowed-address": IDENT.client_address, "comment": IDENT.peer_name}
    row.update(over)
    return row


print("--- read: the four-part identity ---")
check("no such peer -> absent", wg.read(FakeRouter(), IDENT).state, ReadState.ABSENT)
check("all four parts equal -> present_match", wg.read(FakeRouter(peers=[peer()]), IDENT).state, ReadState.PRESENT_MATCH)
check("the handle is the RouterOS .id of that very peer", wg.read(FakeRouter(peers=[peer()]), IDENT).handle, "*7")
for label, row, code in (
    ("LP-39: same comment, another public key", peer(**{"public-key": "OTHER"}), "conflict_name_taken"),
    ("same public key, another comment", peer(comment="someone-else"), "conflict_public_key_taken"),
    ("same name and key, another allowed-address", peer(**{"allowed-address": "10.66.66.9/32"}), "conflict_address_mismatch"),
):
    result = wg.read(FakeRouter(peers=[row]), IDENT)
    check(f"{label} -> present_conflict ({code})", (result.state, result.conflict_code), (ReadState.PRESENT_CONFLICT, code))
two = wg.read(FakeRouter(peers=[peer(**{"public-key": "OTHER"}), peer(**{".id": "*8", "comment": "x"})]), IDENT)
check("name on one peer and key on another -> present_conflict", (two.state, two.conflict_code),
      (ReadState.PRESENT_CONFLICT, "conflict_multiple_peers"))
check("a peer on ANOTHER interface is not ours", wg.read(FakeRouter(peers=[peer(interface="wg1")]), IDENT).state, ReadState.ABSENT)
broken = FakeRouter(peers=[peer()])
broken.fail["list_peers"] = OSError("connection reset")
check("the router cannot be read -> unreadable (never 'absent')", wg.read(broken, IDENT).state, ReadState.UNREADABLE)
multi = wg.WireguardIdentity("wg0", "user-ali-abc123", "PUBKEY-A", "10.66.66.2/32,10.66.66.3/32")
check("a multi-address peer matches regardless of spacing",
      wg.read(FakeRouter(peers=[peer(**{"allowed-address": "10.66.66.2/32, 10.66.66.3/32"})]), multi).state,
      ReadState.PRESENT_MATCH)

print("--- ensure_present ---")
router = FakeRouter(addresses=["10.66.66.1/24"])
first = wg.ensure_present(router, IDENT, SUBNET)
check("absent -> one add_peer, reported as created", (first.created, router.writes()),
      (True, ["ensure_wireguard_interface", "add_peer"]))
check("the peer carries exactly the stored identity",
      [(p["public-key"], p["allowed-address"], p["comment"]) for p in router.peers],
      [("PUBKEY-A", "10.66.66.2/32", "user-ali-abc123")])
router.log.clear()
second = wg.ensure_present(router, IDENT, SUBNET)
check("LP-38: a second ensure_present adds nothing (still one peer)",
      (second.created, router.writes(), len(router.peers)), (False, ["ensure_wireguard_interface"], 1))

limited = wg.WireguardIdentity("wg0", "user-ali-abc123", "PUBKEY-A", "10.66.66.2/32", speed_limit_mbps=20)
router = FakeRouter(addresses=["10.66.66.1/24"])
wg.ensure_present(router, limited, SUBNET)
check("with a speed limit the queue is upserted under the peer's queue name",
      router.queues, {"um-speed-user-ali-abc123": ("10.66.66.2/32", 20)})
check("the queue name is the one the legacy code uses",
      wg.speed_queue_name("user-ali-abc123"), user_ops.wg_speed_queue_name("user-ali-abc123"))

conflicted = FakeRouter(addresses=["10.66.66.1/24"], peers=[peer(**{"public-key": "OTHER"})])
check("conflict -> AdapterConflict", raises_code(lambda: wg.ensure_present(conflicted, IDENT, SUBNET), AdapterConflict),
      "conflict_name_taken")
check("...and NOTHING was added, removed or changed", ([w for w in conflicted.writes() if w != "ensure_wireguard_interface"],
                                                     conflicted.peers), ([], [peer(**{"public-key": "OTHER"})]))
check("a conflict is permanent (retrying cannot help)", AdapterConflict.retryable, False)

unreadable = FakeRouter(addresses=["10.66.66.1/24"])
unreadable.fail["list_peers"] = OSError("timeout")
check("unreadable -> a retryable error, nothing added",
      (raises_code(lambda: wg.ensure_present(unreadable, IDENT, SUBNET)), "add_peer" in unreadable.writes()),
      ("wg_unreadable", False))

flaky = FakeRouter(addresses=["10.66.66.1/24"])
flaky.fail["add_peer"] = RuntimeError("the secret is S3CR3T")
error = None
try:
    wg.ensure_present(flaky, IDENT, SUBNET)
except AdapterError as exc:
    error = exc
check("add_peer failing -> retryable error whose text is only the exception TYPE",
      (error.code, error.retryable, str(error)), ("wg_add_peer_failed", True, "RuntimeError"))

ghost = FakeRouter(addresses=["10.66.66.1/24"])
ghost.add_peer = lambda *a, **k: ghost.log.append(("add_peer",))        # "succeeds" but nothing appears
check("add_peer that reports success but is not there on re-read -> not treated as success",
      raises_code(lambda: wg.ensure_present(ghost, IDENT, SUBNET)), "wg_add_peer_unconfirmed")

outside = wg.WireguardIdentity("wg0", "user-x", "PUBKEY-X", "10.66.70.2/32")
far = FakeRouter(addresses=["10.66.66.1/24"])
check("a client address outside the node's subnet -> permanent error, no peer",
      (raises_code(lambda: wg.ensure_present(far, outside, SUBNET), AdapterPermanentError), far.peers),
      ("wg_ip_outside_subnet", []))

print("--- interface address: only toward the given subnet, only wider ---")
fix = wg.ensure_interface_address_exact_or_wider
r = FakeRouter()
check("router has no address -> the gateway address is added",
      (fix(r, "wg0", "10.66.66.0/24"), r.addresses), ("added", ["10.66.66.1/24"]))
r = FakeRouter(addresses=["10.66.66.1/24"])
check("router equals the subnet -> nothing written",
      (fix(r, "wg0", "10.66.66.0/24"), r.writes()), ("in_sync", []))
r = FakeRouter(addresses=["10.66.66.1/24"])
check("LP-79: panel grew to /23, router still /24 -> repaired to /23",
      (fix(r, "wg0", "10.66.66.0/23"), r.addresses), ("repaired", ["10.66.66.1/23"]))
r = FakeRouter(addresses=["10.66.66.1/23"])
check("LP-81: a caller still believing /24 while the router is /23 -> nothing written, router stays /23",
      (fix(r, "wg0", "10.66.66.0/24"), r.writes(), r.addresses), ("router_wider", [], ["10.66.66.1/23"]))
r = FakeRouter(addresses=["10.66.67.1/24"])
check("the upper /24 of the new /23 is also 'narrower' and gets repaired",
      (fix(r, "wg0", "10.66.66.0/23"), r.addresses), ("repaired", ["10.66.66.1/23"]))
r = FakeRouter(addresses=["192.168.5.1/24"])
check("an unrelated network -> permanent conflict, nothing written",
      (raises_code(lambda: fix(r, "wg0", "10.66.66.0/24"), AdapterPermanentError), r.writes()),
      ("wg_interface_address_conflict", []))
r = FakeRouter(addresses=["10.66.66.1/24", "10.99.0.1/24"])
check("more than one address -> permanent conflict, nothing written",
      (raises_code(lambda: fix(r, "wg0", "10.66.66.0/24"), AdapterPermanentError), r.writes()),
      ("wg_interface_address_conflict", []))
r = FakeRouter(addresses=["not-an-address"])
check("an unparseable address -> permanent conflict", raises_code(lambda: fix(r, "wg0", SUBNET), AdapterPermanentError),
      "wg_interface_address_conflict")
r = FakeRouter(addresses=["10.66.66.1/24"])
r.fail["set_interface_address"] = OSError("link down")
check("LP-80: the replacement failing is a RETRYABLE error",
      (raises_code(lambda: fix(r, "wg0", "10.66.66.0/23")), AdapterError.retryable), ("wg_interface_address_failed", True))
r.fail.clear()
check("...and the next attempt repairs it", (fix(r, "wg0", "10.66.66.0/23"), r.addresses), ("repaired", ["10.66.66.1/23"]))
r = FakeRouter(addresses=[])        # a replacement that died after 'remove', before 'add'
check("LP-80: an interface left with NO address is healed by the next attempt",
      (fix(r, "wg0", "10.66.66.0/23"), r.addresses), ("added", ["10.66.66.1/23"]))
r = FakeRouter(addresses=["10.66.66.1/24"])
real_list = r.list_interface_addresses
seen = []


def racing(interface):
    rows = real_list(interface)
    if not seen:
        seen.append(1)
        r.addresses = ["10.66.64.1/22"]      # someone widened it between the two looks
    return rows


r.list_interface_addresses = racing
check("widened by someone else between the two looks -> NOT overwritten with the narrower value",
      (fix(r, "wg0", "10.66.66.0/23"), r.addresses, r.writes()), ("router_wider", ["10.66.64.1/22"], []))

print("--- ensure_present repairs the address before adding the peer (LP-79) ---")
late = wg.WireguardIdentity("wg0", "user-b", "PUBKEY-B", "10.66.67.20/32")     # an address only valid in the /23
r = FakeRouter(addresses=["10.66.66.1/24"])
result = wg.ensure_present(r, late, "10.66.66.0/23")
check("router widened first, then the peer added",
      (r.writes(), r.addresses, result.notes),
      (["ensure_wireguard_interface", "set_interface_address", "add_peer"], ["10.66.66.1/23"], ("address:repaired",)))

print("--- ensure_absent ---")
r = FakeRouter(addresses=["10.66.66.1/24"], peers=[peer(), peer(**{".id": "*9", "public-key": "K2", "comment": "other",
                                                             "allowed-address": "10.66.66.3/32"})])
r.queues[wg.speed_queue_name(IDENT.peer_name)] = ("10.66.66.2/32", 5)
check("LP-40: present -> removed by .id, re-read, verified_absent",
      (wg.ensure_absent(r, IDENT), [p["comment"] for p in r.peers], r.queues),
      (AbsentOutcome.VERIFIED_ABSENT, ["other"], {}))
check("the unrelated peer was not touched", ("remove_peer", "*9") in r.log, False)
check("LP-83: no call on the interface or its address",
      [e[0] for e in r.log if "address" in e[0] or e[0] == "ensure_wireguard_interface"], [])
r.log.clear()
check("LP-40: already absent -> verified_absent again, no remove_peer",
      (wg.ensure_absent(r, IDENT), "remove_peer" in r.writes()), (AbsentOutcome.VERIFIED_ABSENT, False))

r = FakeRouter(peers=[peer(**{"public-key": "OTHER"})])
check("LP-57: a peer with our comment but another key -> unverified, NOT removed",
      (wg.ensure_absent(r, IDENT), r.writes(), len(r.peers)), (AbsentOutcome.UNVERIFIED, [], 1))
r = FakeRouter(peers=[peer()])
r.fail["list_peers"] = OSError("down")
check("router unreadable -> unverified, nothing removed", (wg.ensure_absent(r, IDENT), r.writes()), (AbsentOutcome.UNVERIFIED, []))
r = FakeRouter(peers=[peer()])
r.fail["remove_peer"] = OSError("refused")
check("remove_peer failing -> unverified", wg.ensure_absent(r, IDENT), AbsentOutcome.UNVERIFIED)
r = FakeRouter(peers=[peer()])
r.remove_peer = lambda peer_id: r.log.append(("remove_peer", peer_id))     # "succeeds", peer stays
check("remove_peer that reports success while the peer is still there -> unverified",
      wg.ensure_absent(r, IDENT), AbsentOutcome.UNVERIFIED)
r = FakeRouter(peers=[peer()])
r.fail["remove_simple_queue"] = OSError("queue busy")
check("the queue could not be removed -> unverified (something of ours may remain)",
      wg.ensure_absent(r, IDENT), AbsentOutcome.UNVERIFIED)
check("an adapter never reports 'abandoned' or 'delete_idempotently_absent' for WireGuard (it can always read)",
      sorted(o.value for o in AbsentOutcome), ["delete_idempotently_absent", "unverified", "verified_absent"])

print("--- radius_ppp: no remote at all ---")
check("ensure_present makes no call and creates nothing remote", (ppp.ensure_present().created, ppp.HAS_REMOTE), (False, False))
check("ensure_absent has no remote outcome", ppp.ensure_absent(), None)

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
