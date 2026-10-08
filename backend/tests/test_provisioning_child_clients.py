"""Eight guarded clients constructed without network; fingerprint binding."""
import os
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _no_network
_no_network.install([])
from app.services import provisioning_child_clients as factory
from app.services import provisioning_child_authority as authority
from app.services.provisioning_child_http import READ_POSTS


public = dict.fromkeys(factory.rules.ENDPOINT_FIELDS)
public.update(type="xray", xr_panel_mode="ssh", mt_host="192.0.2.1", mt_port=8728, mt_use_ssl=False,
    mt_api_ssl_port=8729, mt_wireguard_interface="wg0", xr_ssh_host="192.0.2.2", xr_ssh_port=22,
    xr_panel_base_url="https://example.invalid/panel", se_host="192.0.2.3", se_port=443, se_hub_name="hub",
    xr_inbound_tag="inbound", xr_panel_inbound_id=1, xr_config_path="/etc/xray/config.json", xr_service_name="xray")
fingerprint = factory.rules.endpoint_fingerprint({"n_" + key: value for key, value in public.items()})
credentials = {key: "SECRET_SENTINEL" for key in factory.SECRET_FIELDS}
credentials["xr_ssh_private_key"] = None
for backend in ("mikrotik_wg", "softether", "xray_ssh", "threexui", "marzban", "marzneshin", "hiddify", "sui"):
    binding = SimpleNamespace(node_id=7, backend=backend, phase="forward")
    guard = object.__new__(factory.ChildGuard)  # Explicit unit proof fixture, not a host certificate.
    guard.check = lambda: binding
    guard._contract = json.dumps({"config_fingerprint": fingerprint})
    with patch.object(authority, "_require_guard") as proof:
        client = factory.build(guard, public, credentials)
        assert proof.call_args.args == (guard,)
    try:
        if backend in READ_POSTS:
            if backend == "sui":
                with patch.object(client, "test_connection"):
                    client.connect()
            assert isinstance(client.session, factory.ChildHttpSession)
            try:
                client.session.post(client.base_url + "/unregistered-writer")
                raise AssertionError("factory client used an unguarded session")
            except authority.WriteAuthorityUnavailable:
                pass
    finally:
        client.close()
with patch.object(authority, "_require_guard"):
    for invalid in ({**public, "xr_panel_base_url": "https://other.invalid"}, {**public, "unexpected": 1}):
        try:
            factory.build(guard, invalid, credentials)
            raise AssertionError("invalid snapshot accepted")
        except factory.ChildClientUnavailable as exc:
            assert "SECRET_SENTINEL" not in str(exc)
    try:
        factory.build(guard, public, {"untrusted_extra": "SECRET_SENTINEL"})
        raise AssertionError("unknown credential field accepted")
    except factory.ChildClientUnavailable:
        pass
assert _no_network.attempts == []
print("PASS eight private clients: fresh node proof, endpoint fingerprint, closed credentials, guarded HTTP and deferred SUI session")
