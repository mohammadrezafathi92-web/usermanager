"""A test that drives real endpoint code must not reach a node.

install() makes every outgoing socket connection fail at once and records
the attempt, except to hosts the test explicitly allows (the MariaDB server
of MARIADB_TEST_URL). A test then asserts `attempts == []`: code that would
have dialled a MikroTik, an Xray panel or Telegram is caught, instead of the
test silently depending on a timeout or on whatever answers at that address.

Stub the function that would dial out (for example
user_ops.get_connection_share) and keep the guard as the proof that the stub
covers everything.
"""
from __future__ import annotations

import socket
from typing import Iterable

from sqlalchemy.engine import make_url

attempts: list = []
_allowed: set = set()
_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex
_installed = False
_LOCAL = ("127.0.0.1", "::1", "localhost")


def _is_allowed(address) -> bool:
    if not isinstance(address, tuple) or len(address) < 2:
        return True                          # unix sockets and the like: local by nature
    host, port = address[0], address[1]
    if (host, port) in _allowed:
        return True
    return host in _LOCAL and (None, port) in _allowed


def _guarded_connect(self, address):
    if _is_allowed(address):
        return _real_connect(self, address)
    attempts.append(address)
    raise OSError(f"network access is not allowed in this test: {address!r}")


def _guarded_connect_ex(self, address):
    if _is_allowed(address):
        return _real_connect_ex(self, address)
    attempts.append(address)
    return 111                               # ECONNREFUSED


def allow_database(url: str) -> None:
    """Lets connections to the database server of this URL through."""
    parsed = make_url(url)
    if parsed.host:
        port = parsed.port or 3306
        _allowed.add((parsed.host, port))
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
        _installed = True
