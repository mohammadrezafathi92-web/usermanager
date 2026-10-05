"""A test that drives real endpoint code must not reach a node.

install() refuses, at once, and records every way a Python socket can send
something to another host - except to hosts the test explicitly allows (the
MariaDB server of MARIADB_TEST_URL):

    connect / connect_ex      TCP, and "connected" UDP
    sendto / sendmsg          unconnected UDP (RADIUS, DNS, syslog ...)
    getaddrinfo               a name lookup is itself a network request

A test then asserts `attempts == []`: code that would have dialled a
MikroTik, an Xray panel or Telegram is caught, instead of the test silently
depending on a timeout or on whatever answers at that address.

Limits, stated rather than implied: this guards Python's own `socket`
module in this process. It does not see a child process, or a C extension
that opens sockets without going through Python.

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
_LOCAL = ("127.0.0.1", "::1", "localhost")

_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_real_sendto = socket.socket.sendto
_real_sendmsg = getattr(socket.socket, "sendmsg", None)
_real_getaddrinfo = socket.getaddrinfo


class NetworkRefused(OSError):
    pass


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


def _guarded_connect(self, address):
    if _is_allowed(address):
        return _real_connect(self, address)
    raise _refuse(address)


def _guarded_connect_ex(self, address):
    if _is_allowed(address):
        return _real_connect_ex(self, address)
    _refuse(address)
    return 111                               # ECONNREFUSED


def _guarded_sendto(self, data, *args):
    address = args[-1] if args else None     # sendto(data, address) or sendto(data, flags, address)
    if address is None or _is_allowed(address):
        return _real_sendto(self, data, *args)
    raise _refuse(address)


def _guarded_sendmsg(self, buffers, ancdata=(), flags=0, address=None):
    if address is None or _is_allowed(address):  # no address: an already-connected socket, checked at connect
        return _real_sendmsg(self, buffers, ancdata, flags, address)
    raise _refuse(address)


def _is_literal_ip(host) -> bool:
    try:
        ipaddress.ip_address(host.decode() if isinstance(host, bytes) else host)
        return True
    except (ValueError, AttributeError):
        return False


def _guarded_getaddrinfo(host, port, *args, **kwargs):
    # A literal address or a local name needs no lookup on the network;
    # anything else would ask a DNS server.
    if host is None or _is_literal_ip(host) or host in _LOCAL or host in _allowed_hosts:
        return _real_getaddrinfo(host, port, *args, **kwargs)
    attempts.append((host, port))
    raise socket.gaierror(socket.EAI_NONAME, f"name lookup is not allowed in this test: {host!r}")


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
        socket.socket.connect = _guarded_connect
        socket.socket.connect_ex = _guarded_connect_ex
        socket.socket.sendto = _guarded_sendto
        if _real_sendmsg is not None:
            socket.socket.sendmsg = _guarded_sendmsg
        socket.getaddrinfo = _guarded_getaddrinfo
        _installed = True
