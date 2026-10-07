"""Real librouteros Path/Query through recording API, never a socket."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from librouteros.query import Key
from app.services import provisioning_child_routeros as router
from app.services import provisioning_child_authority as authority


class Raw:
    def __init__(self):
        self.sent = []
    def __call__(self, command, **kwargs):
        self.sent.append((command, kwargs))
        yield {"ret": "*1"}
    def rawCmd(self, command, *words):
        self.sent.append((command, words))
        yield {".id": "*1", "name": "test"}
    def close(self):
        pass


def refused(callback):
    try:
        callback()
        raise AssertionError("unguarded RouterOS mutation accepted")
    except (authority.WriteAuthorityUnavailable, AttributeError):
        pass


raw = Raw()
api = router.ChildRouterosApi(raw, 7)
path = api.path("interface", "wireguard", "peers")
assert path.api is api
assert list(path)[0]["ret"] == "*1"
assert list(path.select(Key(".id"), Key("name")).where(Key("name") == "test"))[0]["name"] == "test"
assert len(raw.sent) == 2 and not api.write_attempted
for write in (lambda: path.add(name="test"), lambda: path.update(**{".id": "*1"}),
              lambda: path.remove("*1"), lambda: list(path("future-command")),
              lambda: list(api("/tool/fetch", url="https://example.invalid")),
              lambda: list(api("/system/resource/print", file="writes-a-file")),
              lambda: list(api.rawCmd("/system/resource/print", "=file=writes-a-file")),
              lambda: list(api.rawCmd("/system/reboot"))):
    refused(write)
assert len(raw.sent) == 2 and not api.write_attempted
refused(lambda: api.protocol.writeSentence("/system/reboot"))
deferred = None
with patch.object(authority, "require_write") as proof:
    assert path.add(name="test") == "*1"
    path.update(**{".id": "*1"})
    path.remove("*1")
    assert proof.call_count == 3 and proof.call_args.args == (7, "mikrotik_wg")
    deferred = api("/system/reboot")
    assert proof.call_count == 3  # Construction is not dispatch.
before = len(raw.sent)
refused(lambda: list(deferred))
assert len(raw.sent) == before  # Fresh proof at actual generator consumption.
assert api.write_attempted
for node_id in (None, True, 0, "7"):
    refused(lambda node_id=node_id: router.ChildRouterosApi(raw, node_id))
assert _no_network.attempts == []
print("PASS private RouterOS: real Path/Query reads, all writes/commands fenced, print-file denial, deferred dispatch and raw protocol denial")
