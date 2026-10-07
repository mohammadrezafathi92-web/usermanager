"""Private librouteros guard; deferred commands revalidate at consumption.

Uses the library's real Path/Query with THIS API, never the underlying API.
Print/select/iteration are reads only without write-like print arguments.
No live client replacement, real action registration or mode activation.
"""
import re

from librouteros.api import Path

from .mikrotik_client import MikrotikClient
from . import provisioning_child_authority as authority


def _print_command(command):
    return isinstance(command, str) and bool(re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)+print", command))


class ChildRouterosApi:
    def __init__(self, raw, node_id):
        if type(node_id) is not int or node_id < 1:
            raise authority.WriteAuthorityUnavailable("child_routeros_binding_invalid")
        self._raw, self._node_id = raw, node_id
        self.write_attempted = False

    def _write(self):
        authority.require_write(self._node_id, "mikrotik_wg")
        self.write_attempted = True

    def __call__(self, command, **kwargs):
        # Generator body starts just before the underlying library's send,
        # not when a caller merely constructs an iterator.
        if not _print_command(command) or set(kwargs) - {".proplist"}:
            self._write()
        yield from self._raw(command, **kwargs)

    def rawCmd(self, command, *words):
        if not _print_command(command) or any(not isinstance(word, str) or not (
                word.startswith("?") or word.startswith("=.proplist=")) for word in words):
            self._write()
        yield from self._raw.rawCmd(command, *words)

    def path(self, *path):
        return Path(path="", api=self).join(*path)

    def close(self):
        self._raw.close()


class ChildMikrotikClient(MikrotikClient):
    def __init__(self, *args, node_id, **kwargs):
        ChildRouterosApi(None, node_id)  # Validate before authentication/network.
        super().__init__(*args, **kwargs)
        self._bound_node_id = node_id

    @classmethod
    def for_node(cls, node):
        port = node.mt_api_ssl_port if node.mt_use_ssl else node.mt_port
        return cls(node.mt_host, node.mt_username, node.mt_password, port, node.mt_use_ssl, node_id=node.id)

    def connect(self):
        super().connect()
        self._api = ChildRouterosApi(self._api, self._bound_node_id)
        return self
