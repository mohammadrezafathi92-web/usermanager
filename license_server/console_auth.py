"""Console sessions, CSRF and login throttling.

One operator, one password (store.check_admin_password). Sessions stay in
memory: a restart only means logging in again. The cookie holds a random
token; the server keeps only its digest, an idle and an absolute deadline,
and the CSRF token every state-changing form must echo back.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

COOKIE = "lic_console"
IDLE_SECONDS = 2 * 3600
ABSOLUTE_SECONDS = 12 * 3600

# Failed logins: per client address, and in total so many addresses cannot
# share one unlimited budget.
FAIL_WINDOW = 15 * 60
FAIL_PER_IP = 5
FAIL_TOTAL = 50
MIN_PASSWORD_LENGTH = 12


@dataclass
class Session:
    created: float
    last: float
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24))
    # review-page nonce -> None while unused, the licence id once signed:
    # a double click or a resend cannot sign a second licence.
    issues: dict = field(default_factory=dict)


_lock = threading.Lock()
_sessions: dict[str, Session] = {}
_failures: list[tuple[float, str]] = []


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create(now: Optional[float] = None) -> tuple[str, Session]:
    now = now or time.time()
    token = secrets.token_urlsafe(32)
    session = Session(created=now, last=now)
    with _lock:
        _sessions[_digest(token)] = session
    return token, session


def get(token: Optional[str], now: Optional[float] = None) -> Optional[Session]:
    if not token:
        return None
    now = now or time.time()
    key = _digest(token)
    with _lock:
        session = _sessions.get(key)
        if session is None:
            return None
        if now - session.last > IDLE_SECONDS or now - session.created > ABSOLUTE_SECONDS:
            _sessions.pop(key, None)
            return None
        session.last = now
        return session


def drop(token: Optional[str]) -> None:
    if token:
        with _lock:
            _sessions.pop(_digest(token), None)


def drop_all() -> None:
    """After a password change: every other browser has to log in again."""
    with _lock:
        _sessions.clear()


def csrf_ok(session: Optional[Session], value: Optional[str]) -> bool:
    return bool(session and value and secrets.compare_digest(session.csrf, value))


def login_blocked(ip: str, now: Optional[float] = None) -> bool:
    now = now or time.time()
    with _lock:
        _failures[:] = [(t, a) for t, a in _failures if now - t < FAIL_WINDOW]
        mine = sum(1 for _t, a in _failures if a == ip)
        return mine >= FAIL_PER_IP or len(_failures) >= FAIL_TOTAL


def login_failed(ip: str, now: Optional[float] = None) -> None:
    with _lock:
        _failures.append((now or time.time(), ip))


def login_succeeded(ip: str) -> None:
    with _lock:
        _failures[:] = [(t, a) for t, a in _failures if a != ip]


def reset() -> None:
    """Tests only."""
    with _lock:
        _sessions.clear()
        _failures.clear()
