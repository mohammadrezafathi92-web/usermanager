"""Read-only node contract guards; no probe, ready writer or activation."""
import hashlib
import json
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from .adapter_base import InvalidMatcher, validate_matchers
from .provisioning_identity import PPP, XRAY_MODES

_FILES = ("adapter_base.py", "adapter_mikrotik_wg.py", "adapter_radius_ppp.py",
          "adapter_softether.py", "adapter_xray.py", "adapter_xray_panels.py", "provisioning_contracts.py")
_FIELDS = ("type", "xr_panel_mode", "mt_host", "mt_port", "mt_use_ssl", "mt_api_ssl_port",
           "mt_wireguard_interface", "xr_ssh_host", "xr_ssh_port", "xr_panel_base_url",
           "se_host", "se_port", "se_hub_name", "xr_inbound_tag", "xr_panel_inbound_id",
           "xr_config_path", "xr_service_name")


def adapter_version():
    # Includes adapter helpers as well as the entry functions. Changing any
    # adapter conservatively invalidates the previous version for all nodes.
    return hashlib.sha1(b"".join((Path(__file__).parent / name).read_bytes() for name in _FILES)).hexdigest()


def config_fingerprint(node):
    values = {name: getattr(node, name, None) for name in _FIELDS}
    values["type"] = node.type.value
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def backend_for(node, protocol):
    if node.type == models.NodeType.mikrotik:
        if protocol == "wireguard":
            return "mikrotik_wg"
        if protocol in PPP:
            return "radius_ppp"
    if node.type == models.NodeType.softether and protocol == "softether":
        return "softether"
    if node.type == models.NodeType.xray and protocol == "xray":
        for backend, mode in XRAY_MODES.items():
            if (node.xr_panel_mode or "ssh") == mode:
                return backend
    raise HTTPException(409, "provisioning_backend_invalid")


def require_ready(db, node, backend):
    if backend == "radius_ppp":
        return None
    row = db.execute(select(mp.ProvisioningNodeContract).where(
        mp.ProvisioningNodeContract.node_id == node.id, mp.ProvisioningNodeContract.backend == backend)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None or row.state != "ready" or row.adapter_version != adapter_version() or (
            row.config_fingerprint != config_fingerprint(node)):
        raise HTTPException(409, "node_contract_not_ready")
    try:
        contract = json.loads(row.contract)
        if not isinstance(contract, dict) or "not_exist" not in contract:
            raise ValueError()
        matchers = validate_matchers(contract["not_exist"])
        for matcher in matchers:
            if matcher["kind"] == "http" and (backend not in (
                    "threexui", "marzban", "hiddify", "marzneshin", "sui") or matcher["json_path"] not in (
                    "detail", "message", "msg", "error", "error.message", "error.code") or
                    type(matcher["equals"]) not in (str, int) or not 100 <= matcher["status"] <= 599):
                raise ValueError()
            if matcher["kind"] == "jsonrpc" and backend != "softether":
                raise ValueError()
    except (ValueError, TypeError, InvalidMatcher):
        raise HTTPException(409, "node_contract_not_ready") from None
    return contract
