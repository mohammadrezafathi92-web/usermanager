"""Private SoftEther client: guarded RPC and underlying HTTP send.

Not used by live legacy callers and not a registered real runner action.
Only the four documented reads are exempt; unknown RPC methods are writers.
Direct session sends cannot bypass method, hub, destination or authority proof.
"""
import json

import requests

from .softether_client import SoftEtherClient
from . import provisioning_child_authority as authority
from .provisioning_child_http import _destination, ChildHttpUnavailable

READS = frozenset(("GetHubStatus", "EnumUser", "GetUser", "EnumSession"))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


class _RpcSession(requests.Session):
    def __init__(self, base_url, hub_name, node_id, timeout):
        super().__init__()
        self.trust_env = False
        self._destination = _destination(base_url)
        self._hub_name, self._node_id = hub_name, node_id
        self._timeout, self._write_attempted = timeout, False

    def send(self, request, **kwargs):
        if _destination(request.url) != self._destination or request.method != "POST" or kwargs.get("proxies"):
            raise ChildHttpUnavailable("child_rpc_destination_invalid")
        try:
            body = json.loads(request.body, object_pairs_hook=_unique_object)
            if not isinstance(body, dict) or set(body) != {"jsonrpc", "id", "method", "params"} or (
                    type(body.get("id")) not in (str, int)) or body.get("jsonrpc") != "2.0" or not isinstance(body.get("method"), str) or (
                    not body["method"] or not isinstance(body.get("params"), dict)) or (
                    body["params"].get("HubName_str") != self._hub_name or
                    request.headers.get("X-VPNADMIN-HUBNAME") != self._hub_name):
                raise ValueError()
        except (TypeError, ValueError):
            raise ChildHttpUnavailable("child_rpc_envelope_invalid") from None
        writing = body["method"] not in READS
        if writing:
            authority.require_write(self._node_id, "softether")
        kwargs["timeout"], kwargs["proxies"] = self._timeout, {}
        self.get_adapter(request.url)
        if writing:
            self._write_attempted = True
        return super().send(request, **kwargs)


class ChildSoftEtherClient(SoftEtherClient):
    def __init__(self, *args, node_id, connect_timeout=10, read_timeout=15, **kwargs):
        if type(node_id) is not int or node_id < 1 or any(type(value) not in (int, float) or not 0 < value <= 120
                for value in (connect_timeout, read_timeout)):
            raise ChildHttpUnavailable("child_rpc_binding_invalid")
        super().__init__(*args, **kwargs)
        headers = dict(self.session.headers)
        self.session.close()
        self.session = _RpcSession(self.base_url, self.hub_name, node_id, (connect_timeout, read_timeout))
        self.session.headers.update(headers)
        self._bound_node_id = node_id

    @property
    def write_attempted(self):
        return self.session._write_attempted

    def _call(self, method, params=None):
        if method not in READS:
            authority.require_write(self._bound_node_id, "softether")
        return super()._call(method, params)
