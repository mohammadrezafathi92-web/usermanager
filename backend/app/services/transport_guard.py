"""Runtime write guard of the LEGACY remote clients (Lifecycle 10.7).

The AST inventory (remote_writers.py) finds writer call sites by name; this
module is the defence that does not depend on names. It sits in the lowest
layer every legacy client sends through - the RouterOS API object, the SSH
channel/SFTP of the Xray SSH client, the requests Session of the five HTTP
panels and the SoftEther JSON-RPC call - and classifies each send as a read
or a WRITE (default-deny: anything not explicitly a read is a write).

Before any byte of a write leaves the process:

  inside the runner child   the child's own write authority must accept it
                            (provisioning_child_authority.require_write: fresh
                            node lock, advisory lock, installation, ownership
                            epoch and binding). A legacy client used in the
                            child without that authority is refused.
  elsewhere, gate 'off'     nothing happens: today's behaviour, unchanged.
  elsewhere, 'shadow'       allowed; a write that did not come through
                            node_gate for this node (no context, or a context
                            of another node) is counted as
                            writer_not_instrumented - the blocker of
                            'enforced' (10.6).
  elsewhere, 'enforced'     refused (GateViolation). In 'enforced' a write
                            runs only in the runner (10.8).

A write with a node_gate context of the SAME node was started under the
shared gate-mode lock, so the mode cannot have changed under it. A write
WITHOUT such a context reads the mode fresh from the database (no cache), so
the moment the gate becomes 'enforced' no stale cached 'off' lets it through.
A client built without a saved node (bound_node_id None) is never a
legitimate writer outside 'off'.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

HTTP_READ_POSTS = {
    "threexui": frozenset(("/login", "/panel/api/inbounds/onlines")),
    "marzban": frozenset(("/api/admin/token",)),
    "marzneshin": frozenset(("/api/admins/token",)),
    "hiddify": frozenset(),
    "sui": frozenset(),
}
SOFTETHER_READS = frozenset(("GetHubStatus", "EnumUser", "GetUser", "EnumSession"))


class GateViolation(RuntimeError):
    """A remote write that the gate does not allow; nothing was sent."""


def _in_runner() -> bool:
    from . import provisioning_child_authority as authority
    role = authority._runner.get()
    return role is not None and role[:2] == (os.getpid(), threading.get_ident())


def _fresh_mode() -> Optional[str]:
    """The persisted gate mode, read now (no cache). None when unreadable."""
    from . import node_gate
    try:
        runtime = node_gate.read_runtime()
    except node_gate.GateNotAvailable:
        return node_gate.MODE_ENFORCED            # an unverified strict gate never relaxes
    if runtime is not None:
        return runtime.gate_mode
    cached = node_gate._cached_runtime            # unreadable now: never weaker than what we last saw
    return cached.gate_mode if cached is not None else None


def _node(owner) -> Optional[int]:
    """The saved node a client was built for (legacy factories set
    bound_node_id; the runner's child clients carry _bound_node_id)."""
    value = getattr(owner, "bound_node_id", None)
    return value if value is not None else getattr(owner, "_bound_node_id", None)


def require(node_id: Optional[int], backend: str) -> None:
    """Called immediately before a classified WRITE leaves the process."""
    if _in_runner():
        from . import provisioning_child_authority as authority
        if type(node_id) is not int or node_id <= 0:
            raise GateViolation("runner write from a client without a node")
        authority.require_write(node_id, backend)
        return
    from . import node_gate
    context = node_gate.current_context()
    if context is not None and type(node_id) is int and context.node_id == node_id:
        return                                    # instrumented passage, under the shared mode lock
    mode = _fresh_mode()
    if mode is None or mode == node_gate.MODE_OFF:
        return
    if mode == node_gate.MODE_SHADOW:
        node_gate.counters.count_not_instrumented(node_id if type(node_id) is int and node_id > 0 else 0)
        return
    raise GateViolation("remote write outside the runner while the gate is enforced")


# ------------------------------------------------------------------ RouterOS
def _print_command(command) -> bool:
    return isinstance(command, str) and bool(re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+print", command))


class RouterosApi:
    """Wraps the librouteros API object of a MikrotikClient."""

    def __init__(self, raw, owner):
        self._raw, self._owner = raw, owner

    def __call__(self, command, **kwargs):
        if not _print_command(command) or set(kwargs) - {".proplist"}:
            require(_node(self._owner), "mikrotik_wg")
        yield from self._raw(command, **kwargs)

    def rawCmd(self, command, *words):  # noqa: N802 - librouteros' name
        if not _print_command(command) or any(not isinstance(word, str) or not (
                word.startswith("?") or word.startswith("=.proplist=")) for word in words):
            require(_node(self._owner), "mikrotik_wg")
        yield from self._raw.rawCmd(command, *words)

    def path(self, *path):
        from librouteros.api import Path
        return Path(path="", api=self).join(*path)

    def close(self):
        return self._raw.close()


# ------------------------------------------------------------------ Xray over SSH
_SSH_READS = (
    re.compile(r"test -f /[A-Za-z0-9_./-]+ && echo __FOUND__"),
    re.compile(r"systemctl show -p ExecStart --no-pager [A-Za-z0-9_][A-Za-z0-9_.@-]{0,127} 2>/dev/null; true"),
    re.compile(r"xray api statsquery -s [A-Za-z0-9_.:\[\]-]+ -pattern 'user>>>' -json"),
    re.compile(re.escape("find / -xdev -maxdepth 6 -iname 'config.json' "
                         "\\( -ipath '*xray*' -o -ipath '*v2ray*' \\) 2>/dev/null | head -5")),
)


class _GuardedFile:
    def __init__(self, raw, owner, writing):
        self._raw, self._owner, self._writing = raw, owner, writing

    def _check(self):
        if self._writing:
            require(_node(self._owner), "xray_ssh")

    def read(self, *args):
        return self._raw.read(*args)

    def readline(self, *args):
        return self._raw.readline(*args)

    def write(self, data):
        self._check()
        return self._raw.write(data)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        self._check()
        return self._raw.flush()

    def __getattr__(self, name):                  # stat/chmod/etc. are rare; treat as writes
        attribute = getattr(self._raw, name)
        if callable(attribute):
            def guarded(*args, **kwargs):
                require(_node(self._owner), "xray_ssh")
                return attribute(*args, **kwargs)
            return guarded
        return attribute

    def close(self):
        return self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


class _GuardedSftp:
    def __init__(self, raw, owner):
        self._raw, self._owner = raw, owner

    def open(self, filename, mode="r", bufsize=-1):
        writing = not (isinstance(mode, str) and mode in ("r", "rb"))
        if writing:
            require(_node(self._owner), "xray_ssh")
        return _GuardedFile(self._raw.open(filename, mode, bufsize), self._owner, writing)

    file = open

    def stat(self, path):
        return self._raw.stat(path)

    def close(self):
        return self._raw.close()

    def __getattr__(self, name):                  # rename/remove/chmod/put...: writes
        attribute = getattr(self._raw, name)
        if callable(attribute):
            def guarded(*args, **kwargs):
                require(_node(self._owner), "xray_ssh")
                return attribute(*args, **kwargs)
            return guarded
        return attribute


class SshTransport:
    """Wraps the paramiko SSHClient of an XrayClient."""

    def __init__(self, raw, owner):
        self._raw, self._owner = raw, owner

    def exec_command(self, command, **kwargs):
        if not isinstance(command, str) or not any(pattern.fullmatch(command) for pattern in _SSH_READS):
            require(_node(self._owner), "xray_ssh")
        return self._raw.exec_command(command, **kwargs)

    def open_sftp(self):
        return _GuardedSftp(self._raw.open_sftp(), self._owner)

    def close(self):
        return self._raw.close()

    def get_transport(self):
        return self._raw.get_transport()


# ------------------------------------------------------------------ HTTP panels
class GuardedSession(requests.Session):
    """The requests Session of 3X-UI, Marzban, Hiddify, Marzneshin and S-UI."""

    def __init__(self, owner, backend):
        if backend not in HTTP_READ_POSTS:
            raise ValueError(f"unknown panel backend {backend!r}")
        super().__init__()
        self._owner, self._backend = owner, backend

    def send(self, request, **kwargs):
        method = (request.method or "").upper()
        read = method in ("GET", "HEAD")
        if method == "POST":
            path = urlsplit(request.url).path or "/"
            base = urlsplit(getattr(self._owner, "base_url", "") or "").path.rstrip("/")
            relative = path[len(base):] if base and path.startswith(base + "/") else path
            read = relative in HTTP_READ_POSTS[self._backend] and not urlsplit(request.url).query
        if not read:
            require(_node(self._owner), self._backend)
        return super().send(request, **kwargs)


def softether_call(owner, method: str) -> None:
    """Before each SoftEther JSON-RPC call."""
    if method not in SOFTETHER_READS:
        require(_node(owner), "softether")
