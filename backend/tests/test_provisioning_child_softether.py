"""Private RPC guard tests; recording adapter only, never a real server."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
import requests
from requests.adapters import BaseAdapter
from app.services import provisioning_child_softether as rpc
from app.services import provisioning_child_authority as authority


class Recorder(BaseAdapter):
    def __init__(self):
        self.calls = []
        self.redirect = None

    def send(self, request, **kwargs):
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.request, response.url = request, request.url
        response.status_code, response._content = 200, b'{"result":{"UserList":[]}}'
        if self.redirect:
            response.status_code = 307
            response.headers["Location"] = self.redirect
            self.redirect = None
        return response

    def close(self):
        pass


def refused(callback):
    try:
        callback()
        raise AssertionError("unguarded RPC accepted")
    except (authority.WriteAuthorityUnavailable, rpc.ChildHttpUnavailable):
        pass


with rpc.ChildSoftEtherClient("example.invalid", 443, "hub", "secret", node_id=7) as client:
    recorder = Recorder()
    client.session.mount("https://", recorder)
    for method in rpc.READS:
        client._call(method, {"HubName_str": "hub"})
    assert len(recorder.calls) == 4 and not client.write_attempted
    for method in ("CreateUser", "DeleteUser", "SetUser", "FutureWrite", "getuser", ""):
        refused(lambda method=method: client._call(method, {"HubName_str": "hub"}))
    assert len(recorder.calls) == 4 and not client.write_attempted
    def request(method="FutureWrite", hub="hub", url=client.base_url):
        return requests.Request("POST", url, headers={"X-VPNADMIN-HUBNAME": hub},
            data=json.dumps({"jsonrpc": "2.0", "id": "1", "method": method, "params": {"HubName_str": hub}})).prepare()
    refused(lambda: client.session.send(request()))  # Direct session bypass still denied.
    for bad in (request("EnumUser", hub="other"), request("EnumUser", url="https://other.invalid/api/"),
                requests.Request("GET", client.base_url).prepare(),
                requests.Request("POST", client.base_url, data="not-json").prepare()):
        refused(lambda bad=bad: client.session.send(bad))
    assert len(recorder.calls) == 4
    duplicate = request("EnumUser")
    duplicate.body = '{"jsonrpc":"2.0","id":"1","method":"DeleteUser","method":"EnumUser","params":{"HubName_str":"hub"}}'
    refused(lambda: client.session.send(duplicate))
    assert len(recorder.calls) == 4
    with patch.object(authority, "require_write") as proof:  # Transport unit hook only.
        client.add_client("new", "password")
        assert proof.call_count == 2 and proof.call_args.args == (7, "softether")
        assert client.write_attempted and len(recorder.calls) == 5
        assert recorder.calls[-1][1]["timeout"] == (10, 15)
        recorder.redirect = "https://other.invalid/api/"
        count = len(recorder.calls)
        refused(lambda: client.session.send(request()))
        assert len(recorder.calls) == count + 1
        recorder.redirect = client.base_url
        before = proof.call_count
        client.session.send(request())
        assert proof.call_count == before + 2  # Fresh proof at every redirected send.
    refused(lambda: client.session.send(request("EnumUser"), proxies={"https": "http://other.invalid"}))
assert _no_network.attempts == []
print("PASS private SoftEther: RPC/read allowlist, unknown writes, hub/origin binding, direct send, redirects and deadlines")
