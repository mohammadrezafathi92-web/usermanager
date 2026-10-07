"""No-network HTTP default-deny, read allowlists and redirect checks."""
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
import requests
from requests.adapters import BaseAdapter
from app.services import provisioning_child_http as transport
from app.services import provisioning_child_authority as authority


class RecordingAdapter(BaseAdapter):
    def __init__(self):
        self.sent = []
        self.redirect = None

    def send(self, request, **kwargs):
        self.sent.append((request.method, request.url, kwargs["timeout"]))
        response = requests.Response()
        response.request, response.url = request, request.url
        response.status_code, response._content = 200, b"{}"
        if self.redirect:
            response.status_code = 307
            response.headers["Location"] = self.redirect
            self.redirect = None
        return response

    def close(self):
        pass


def refuses(callback):
    try:
        callback()
        raise AssertionError("unguarded HTTP request accepted")
    except (transport.ChildHttpUnavailable, authority.WriteAuthorityUnavailable):
        pass


for backend, login_reads in transport.READ_POSTS.items():
    with transport.ChildHttpSession("https://example.invalid/panel", 7, backend) as session:
        adapter = RecordingAdapter()
        session.mount("https://", adapter)
        session.get("https://example.invalid/panel/api/users?offset=0")
        for path in login_reads:
            session.post("https://example.invalid/panel" + path)
        assert not session.write_attempted
        count = len(adapter.sent)
        for method in ("POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "CUSTOM"):
            refuses(lambda method=method: session.request(method, "https://example.invalid/panel/api/new_writer"))
        assert len(adapter.sent) == count and not session.write_attempted
        for url in ("https://other.invalid/panel/api/user", "http://example.invalid/panel/api/user",
                    "https://example.invalid:444/panel/api/user", "https://example.invalid/panel-other/api/user",
                    "https://example.invalid/panel/api/../login", "https://example.invalid/panel/api%2flogin",
                    "https://example.invalid/panel//login"):
            prepared = requests.Request("POST", "https://example.invalid/panel/api/user").prepare()
            prepared.url = url  # Exercise raw direct-send paths without Requests normalizing them first.
            refuses(lambda prepared=prepared: session.send(prepared))
        assert len(adapter.sent) == count
        refuses(lambda: session.get("https://example.invalid/panel/api/users", proxies={"https": "http://other.invalid"}))
        assert len(adapter.sent) == count
        with patch.object(authority, "require_write") as proof:  # Unit transport hook, not real authority.
            session.post("https://example.invalid/panel/api/user", timeout=None)
            session.patch("https://example.invalid/panel/api/user", timeout=900)
            assert proof.call_count == 2 and proof.call_args.args == (7, backend)
            assert session.write_attempted and adapter.sent[-1][2] == (10, 15)
            adapter.redirect = "https://other.invalid/panel/api/user"
            count = len(adapter.sent)
            refuses(lambda: session.post("https://example.invalid/panel/api/user"))
            assert len(adapter.sent) == count + 1  # Redirect target never reached adapter.
            adapter.redirect = "https://example.invalid/panel/api/another_writer"
            before = proof.call_count
            session.post("https://example.invalid/panel/api/user")
            assert proof.call_count == before + 2  # Every hop gets fresh proof.
        if login_reads:
            path = next(iter(login_reads))
            count = len(adapter.sent)
            refuses(lambda: session.post("https://example.invalid/panel" + path + "?unexpected=1"))
            assert len(adapter.sent) == count
    print("PASS", backend, "closed destination/read POSTs, unknown writers, direct sends, redirects, fresh proof and timeouts")
with transport.ChildHttpSession("https://example.invalid", None, "sui") as unbound:
    unbound.mount("https://", RecordingAdapter())
    refuses(lambda: unbound.post("https://example.invalid/api/v2/save"))
for bad in ("https://name:secret@example.invalid", "ftp://example.invalid", "https://example.invalid/?a=1"):
    refuses(lambda bad=bad: transport.ChildHttpSession(bad, 1, "sui"))
assert _no_network.attempts == []
