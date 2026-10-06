"""tests/_no_network.py refuses and records every way out - TCP, UDP and
name lookups - and lets only an explicitly allowed database server through.

Run:  python3 backend/tests/test_no_network_guard.py

Everything here stays on this machine: the "remote" addresses are from the
documentation range 192.0.2.0/24 and are never actually contacted (that is
the point), and the one allowed connection goes to a listener this test
opens on 127.0.0.1.
"""
from __future__ import annotations

import os
import socket
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _no_network as guard

failures: list[str] = []
REMOTE = ("192.0.2.10", 8728)             # TEST-NET-1: reserved, routes nowhere


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refused(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except OSError as exc:                 # NetworkRefused and socket.gaierror are both OSError
        return type(exc).__name__
    return None


# Two local listeners for the "what is allowed" checks, opened BEFORE the
# guard (bind/listen are local). Some sandboxes forbid even that; then those
# few checks are skipped and every refusal check below still runs.
try:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    allowed_port = listener.getsockname()[1]
    udp_receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_receiver.bind(("127.0.0.1", 0))
    udp_port = udp_receiver.getsockname()[1]
    udp_receiver.settimeout(2)
    can_listen = True
except OSError as exc:
    print(f"NOTE  cannot open a local listener here ({type(exc).__name__}); the 'allowed' checks are skipped")
    listener = udp_receiver = None
    allowed_port, udp_port, can_listen = 39999, 39998, False

guard.install([f"mysql+pymysql://u:p@127.0.0.1:{allowed_port}/x"])
guard.attempts.clear()

print("--- TCP ---")
tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
check("connect to another host is refused at once and recorded",
      (refused(tcp.connect, REMOTE), guard.attempts), ("NetworkRefused", [REMOTE]))
check("connect_ex reports failure instead of dialling, and is recorded too",
      (tcp.connect_ex(REMOTE) != 0, guard.attempts), (True, [REMOTE, REMOTE]))
check("create_connection (what http and database clients use) is refused the same way",
      (refused(socket.create_connection, REMOTE, 1), guard.attempts[-1]), ("NetworkRefused", REMOTE))
tcp.close()
guard.attempts.clear()

print("--- UDP ---")
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
check("sendto is refused and recorded - nothing leaves (this is what the first version of the guard missed)",
      (refused(udp.sendto, b"radius?", ("192.0.2.10", 1812)), guard.attempts), ("NetworkRefused", [("192.0.2.10", 1812)]))
check("sendto with flags, and sendmsg with an address, are refused too",
      (refused(udp.sendto, b"x", 0, ("192.0.2.11", 53)), refused(udp.sendmsg, [b"x"], (), 0, ("192.0.2.12", 514)),
       guard.attempts[1:]), ("NetworkRefused", "NetworkRefused", [("192.0.2.11", 53), ("192.0.2.12", 514)]))
check("a 'connected' UDP socket is caught at connect", refused(udp.connect, ("192.0.2.13", 1813)), "NetworkRefused")
udp.close()
guard.attempts.clear()

print("--- name lookups ---")
check("resolving a host name is refused and recorded (a lookup is itself a network request)",
      (refused(socket.getaddrinfo, "panel.example.invalid", 443), guard.attempts),
      ("gaierror", [("panel.example.invalid", 443)]))
check("a literal address and a local name need no lookup and still work",
      (refused(socket.getaddrinfo, "192.0.2.10", 80), refused(socket.getaddrinfo, "127.0.0.1", 80),
       refused(socket.getaddrinfo, "localhost", 80), guard.attempts[1:]), (None, None, None, []))
guard.attempts.clear()

print("--- every resolver API, not just getaddrinfo ---")
NAME = "api.telegram.example.invalid"
check("gethostbyname (the project itself calls it, services/wg_tunnel.py) is refused and recorded",
      (refused(socket.gethostbyname, NAME), guard.attempts), ("gaierror", [(NAME, None)]))
check("gethostbyname_ex too", (refused(socket.gethostbyname_ex, NAME), guard.attempts[-1]), ("gaierror", (NAME, None)))
guard.attempts.clear()
check("reverse lookups ask a DNS server even for a literal address: gethostbyaddr and getnameinfo are refused",
      (refused(socket.gethostbyaddr, "192.0.2.10"), refused(socket.getnameinfo, ("192.0.2.10", 80), 0), guard.attempts),
      ("gaierror", "gaierror", [("192.0.2.10", None), ("192.0.2.10", 80)]))
guard.attempts.clear()
check("getfqdn of a remote address goes through gethostbyaddr: no lookup leaves, the attempt is recorded",
      (socket.getfqdn("192.0.2.10"), guard.attempts), ("192.0.2.10", [("192.0.2.10", None)]))
guard.attempts.clear()
check("what needs no DNS server still works: a literal address forward, numeric getnameinfo, local names",
      (refused(socket.gethostbyname, "192.0.2.10"), refused(socket.gethostbyname, "127.0.0.1"),
       refused(socket.getnameinfo, ("192.0.2.10", 80), socket.NI_NUMERICHOST | socket.NI_NUMERICSERV),
       socket.gethostbyname("192.0.2.10"), guard.attempts), (None, None, None, "192.0.2.10", []))

print("--- nothing claimed that is not replaced ---")
check("every function the guard lists is really replaced on this platform",
      [f"{getattr(owner, '__name__', owner)}.{name}" for owner, name in guard.GUARDED
       if hasattr(owner, name) and getattr(owner, name) is guard._real.get((owner, name))], [])
resolver_api = sorted(n for n in dir(socket) if n in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                                                       "getnameinfo"))
check("...and that list covers the socket module's whole name-resolution API",
      sorted(name for owner, name in guard.GUARDED if owner is socket), resolver_api)
check("getfqdn is covered through gethostbyaddr (it is a Python function calling the module's own name)",
      "gethostbyaddr" in socket.getfqdn.__code__.co_names, True)
import ast
app_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
guarded_names = {name for _owner, name in guard.GUARDED} | {"create_connection", "getfqdn", "send", "sendall", "sendfile"}
used = set()
for folder, _dirs, files in os.walk(app_dir):
    for filename in files:
        if filename.endswith(".py"):
            tree = ast.parse(open(os.path.join(folder, filename), encoding="utf-8").read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "socket"
                        and node.attr.startswith(("get", "create_conn"))) and node.attr not in ("gethostname", "getdefaulttimeout"):
                    used.add(node.attr)
check("every socket.* resolver/connector the APPLICATION calls is one the guard stops",
      sorted(used - guarded_names), [])

print("--- what is allowed ---")
if not can_listen:
    print("SKIP  the allowed-database checks (no local listener in this environment)")
    listener = udp_receiver = None
client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
client.settimeout(2)
if can_listen:
    check("the database server named at install() is reachable", refused(client.connect, ("127.0.0.1", allowed_port)), None)
client.close()
other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
check("...but another port on this machine is not",
      (refused(other.connect, ("127.0.0.1", udp_port)), guard.attempts), ("NetworkRefused", [("127.0.0.1", udp_port)]))
other.close()
guard.attempts.clear()
if can_listen:
    guard.allow_database(f"mysql+pymysql://u:p@127.0.0.1:{udp_port}/x")
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.sendto(b"ok", ("127.0.0.1", udp_port))
    check("an allowed address also works over UDP - the guard refuses by address, not by breaking sockets",
          (udp_receiver.recvfrom(16)[0], guard.attempts), (b"ok", []))
    sender.close()
guard.attempts.clear()

print("--- a real client library ---")
import urllib.request
try:
    urllib.request.urlopen("http://192.0.2.10:8080/", timeout=2)
    outcome = "connected"
except Exception as exc:  # noqa: BLE001
    outcome = type(getattr(exc, "reason", exc)).__name__
check("an HTTP request to another host fails immediately and is on the record",
      (outcome, guard.attempts), ("NetworkRefused", [("192.0.2.10", 8080)]))

for opened in (listener, udp_receiver):
    if opened is not None:
        opened.close()
print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
