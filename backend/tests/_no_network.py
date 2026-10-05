"""A test that drives real endpoint code must not reach a node.

install() refuses, at once, and records every way Python's `socket` module
can send something to another host - except to hosts the test explicitly
allows (the MariaDB server of MARIADB_TEST_URL):

    sending          socket.connect, connect_ex          TCP, "connected" UDP
                     socket.sendto, sendmsg(address)     unconnected UDP
                     (send/sendall/sendfile need a connected socket, which
                      is stopped at connect)
    name resolution  getaddrinfo, gethostbyname, gethostbyname_ex,
                     gethostbyaddr, getnameinfo, and getfqdn (which goes
                     through gethostbyaddr)
                     - a lookup is itself a request to a DNS server

GUARDED lists exactly these names and tests/test_no_network_guard.py checks
that every one of them is replaced, so a function added here later cannot
be forgotten in one place and claimed in another.

A test then asserts `attempts == []`: code that would have dialled a
MikroTik, an Xray panel or Telegram is caught, instead of the test silently
depending on a timeout or on whatever answers at that address.

Limits, stated rather than implied: this guards Python's own `socket`
module in this process. It does not see a child process, a C extension
that opens sockets without going through Python, or code that reaches for
the low-level `_socket` module directly.

Stub the function that would dial out (for example
user_ops.get_connection_share) and keep the guard as the proof that the stub
covers everything.
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Iterable

from sqlalchemy.engine import make_url

attempts: list = []
_allowed: set = set()                     # (host, port); (None, port) = that port on this machine
_allowed_hosts: set = set()               # names that may be resolved
_installed = False
_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0", "::"}

# Everything install() replaces: (owner, attribute name).
GUARDED = (
    (socket.socket, "connect"), (socket.socket, "connect_ex"), (socket.socket, "sendto"), (socket.socket, "sendmsg"),
    (socket, "getaddrinfo"), (socket, "gethostbyname"), (socket, "gethostbyname_ex"), (socket, "gethostbyaddr"),
    (socket, "getnameinfo"),
)
_real = {(owner, name): getattr(owner, name) for owner, name in GUARDED if hasattr(owner, name)}


class NetworkRefused(OSError):
    pass


def _text(host):
    return host.decode() if isinstance(host, (bytes, bytearray)) else host


def _is_literal_ip(host) -> bool:
    try:
        ipaddress.ip_address(_text(host))
        return True
    except (ValueError, TypeError):
        return False


def _is_local_name(host) -> bool:
    host = _text(host)
    return host is None or host == "" or host in _LOCAL or host in _allowed_hosts


def _is_allowed(address) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return True                          # unix sockets and the like: local by nature
    host, port = address[0], address[1]
    if (host, port) in _allowed:
        return True
    return host in _LOCAL and (None, port) in _allowed


def _refuse(address):
    attempts.append(address)
    return NetworkRefused(f"network access is not allowed in this test: {address!r}")


def _refuse_lookup(what, port=None):
    attempts.append((_text(what), port))
    return socket.gaierror(socket.EAI_NONAME, f"name lookup is not allowed in this test: {what!r}")


# ------------------------------------------------------------------ sending
def _guarded_connect(self, address):
    if _is_allowed(address):
        return _real[(socket.socket, "connect")](self, address)
    raise _refuse(address)


def _guarded_connect_ex(self, address):
    if _is_allowed(address):
        return _real[(socket.socket, "connect_ex")](self, address)
    _refuse(address)
    return 111                               # ECONNREFUSED


def _guarded_sendto(self, data, *args):
    address = args[-1] if args else None     # sendto(data, address) or sendto(data, flags, address)
    if address is None or _is_allowed(address):
        return _real[(socket.socket, "sendto")](self, data, *args)
    raise _refuse(address)


def _guarded_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if address is None or _is_allowed(address):  # no address: an already-connected socket, checked at connect
        return _real[(socket.socket, "sendmsg")](self, buffers, ancdata, flags, address)
    raise _refuse(address)


# ------------------------------------------------------------------ name resolution
# Forward lookups: a literal address or a local name needs no DNS server.
def _guarded_getaddrinfo(host, port, *args, **kwargs):
    if _is_literal_ip(host) or _is_local_name(host):
        return _real[(socket, "getaddrinfo")](host, port, *args, **kwargs)
    raise _refuse_lookup(host, port)


def _guarded_gethostbyname(hostname):
    if _is_literal_ip(hostname) or _is_local_name(hostname):
        return _real[(socket, "gethostbyname")](hostname)
    raise _refuse_lookup(hostname)


def _guarded_gethostbyname_ex(hostname):
    if _is_literal_ip(hostname) or _is_local_name(hostname):
        return _real[(socket, "gethostbyname_ex")](hostname)
    raise _refuse_lookup(hostname)


# Reverse lookups: asking for the NAME of an address is a DNS request even
# when the address is a literal - only local ones are let through.
def _guarded_gethostbyaddr(ip_address):
    if _is_local_name(ip_address):
        return _real[(socket, "gethostbyaddr")](ip_address)
    raise _refuse_lookup(ip_address)


def _guarded_getnameinfo(sockaddr, flags):
    host = sockaddr[0] if isinstance(sockaddr, tuple) and sockaddr else None
    numeric = bool(flags & socket.NI_NUMERICHOST)      # no name is asked for
    if numeric or _is_local_name(host):
        return _real[(socket, "getnameinfo")](sockaddr, flags)
    raise _refuse_lookup(host, sockaddr[1] if len(sockaddr) > 1 else None)


_REPLACEMENTS = {
    (socket.socket, "connect"): _guarded_connect, (socket.socket, "connect_ex"): _guarded_connect_ex,
    (socket.socket, "sendto"): _guarded_sendto, (socket.socket, "sendmsg"): _guarded_sendmsg,
    (socket, "getaddrinfo"): _guarded_getaddrinfo, (socket, "gethostbyname"): _guarded_gethostbyname,
    (socket, "gethostbyname_ex"): _guarded_gethostbyname_ex, (socket, "gethostbyaddr"): _guarded_gethostbyaddr,
    (socket, "getnameinfo"): _guarded_getnameinfo,
}


def allow_database(url: str) -> None:
    """Lets connections to the database server of this URL through."""
    parsed = make_url(url)
    if parsed.host:
        port = parsed.port or 3306
        _allowed.add((parsed.host, port))
        _allowed_hosts.add(parsed.host)
        if parsed.host in _LOCAL:
            _allowed.add((None, port))


def install(allow_database_urls: Iterable[str] = ()) -> None:
    global _installed
    for url in allow_database_urls:
        if url:
            allow_database(url)
    if not _installed:
        try:
            _LOCAL.add(socket.gethostname())     # this machine's own name is local (a local call, no lookup)
        except Exception:  # noqa: BLE001
            pass
        for key in _real:                    # only what this platform really has
            setattr(key[0], key[1], _REPLACEMENTS[key])
        _installed = True
