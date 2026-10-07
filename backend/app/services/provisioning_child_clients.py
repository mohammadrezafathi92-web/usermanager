"""Private snapshot-bound client factory; no live caller/runner registration.

Public endpoint values must match the current guard's pinned fingerprint.
Management credentials arrive separately from the trusted parent, never
from the child database. All new clients use guarded transports; legacy
client_for_node and live caller behavior are untouched.
"""
from . import provisioning_child_authority as authority
from . import provisioning_contract_rules as rules
from .provisioning_child_http import ChildHttpSession
from .provisioning_child_softether import ChildSoftEtherClient
from .provisioning_child_ssh import ChildXrayClient
from .provisioning_child_routeros import ChildMikrotikClient
from .threexui_client import ThreeXUIClient
from .marzban_client import MarzbanClient
from .marzneshin_client import MarzneshinClient
from .hiddify_client import HiddifyClient
from .sui_client import SuiClient
from .provisioning_child_guard import ChildGuard

SECRET_FIELDS = frozenset(("mt_username", "mt_password", "se_admin_password", "xr_ssh_username",
    "xr_ssh_password", "xr_ssh_private_key", "xr_panel_username", "xr_panel_password", "xr_panel_api_token"))


class ChildClientUnavailable(RuntimeError):
    pass


class _Sui(SuiClient):
    def __init__(self, base_url, api_token, inbound_id, node_id):
        super().__init__(base_url, api_token, inbound_id)
        self._bound_node_id = node_id

    def connect(self):
        if not self.base_url or not self.api_token or not self.inbound_id:
            raise ChildClientUnavailable("child_sui_config_invalid")
        if self.session is not None:
            self.close()
        self.session = ChildHttpSession(self.base_url, self._bound_node_id, "sui")
        self.session.headers["Token"] = self.api_token
        try:
            self.test_connection()
            return self
        except Exception:
            self.close()
            raise


def build(guard, public, credentials):
    if type(guard) is not ChildGuard or not isinstance(public, dict) or set(public) != set(rules.ENDPOINT_FIELDS) or not isinstance(credentials, dict) or (
            set(credentials) - SECRET_FIELDS or any(value is not None and not isinstance(value, str)
            for value in credentials.values())):
        raise ChildClientUnavailable("child_client_snapshot_invalid")
    binding = guard.check()
    authority._require_guard(guard)
    try:
        fingerprint = rules.endpoint_fingerprint({"n_" + key: value for key, value in public.items()})
    except (TypeError, ValueError):
        raise ChildClientUnavailable("child_client_snapshot_invalid") from None
    if fingerprint != guard.contract["config_fingerprint"]:
        raise ChildClientUnavailable("child_client_endpoint_changed")
    node_id, backend = binding.node_id, binding.backend
    secret = lambda key: credentials.get(key) or ""
    if backend == "mikrotik_wg":
        return ChildMikrotikClient(public["mt_host"], secret("mt_username"), secret("mt_password"),
            public["mt_api_ssl_port"] if public["mt_use_ssl"] else public["mt_port"], public["mt_use_ssl"], node_id=node_id)
    if backend == "softether":
        return ChildSoftEtherClient(public["se_host"], public["se_port"], public["se_hub_name"],
            secret("se_admin_password"), node_id=node_id)
    if backend == "xray_ssh":
        # API statistics are not a provisioning input; do not accept an
        # unbound DTO override for the SSH stats address.
        return ChildXrayClient(public["xr_ssh_host"], secret("xr_ssh_username"), public["xr_ssh_port"],
            secret("xr_ssh_password"), secret("xr_ssh_private_key"), public["xr_config_path"],
            public["xr_service_name"], node_id=node_id)
    url, inbound = public["xr_panel_base_url"], public["xr_panel_inbound_id"]
    if backend == "threexui":
        client = ThreeXUIClient(url, secret("xr_panel_username"), secret("xr_panel_password"), inbound,
            api_token=secret("xr_panel_api_token"))
    elif backend == "marzban":
        client = MarzbanClient(url, secret("xr_panel_username"), secret("xr_panel_password"), public["xr_inbound_tag"])
    elif backend == "marzneshin":
        client = MarzneshinClient(url, secret("xr_panel_username"), secret("xr_panel_password"), inbound)
    elif backend == "hiddify":
        client = HiddifyClient(url, secret("xr_panel_api_token"))
    elif backend == "sui":
        return _Sui(url, secret("xr_panel_api_token"), inbound, node_id)
    else:
        raise ChildClientUnavailable("child_client_backend_unsupported")
    try:
        session = ChildHttpSession(client.base_url, node_id, backend)
        session.headers.update(client.session.headers)
    except Exception:
        client.close()
        raise
    client.session.close()
    client.session = session
    return client
