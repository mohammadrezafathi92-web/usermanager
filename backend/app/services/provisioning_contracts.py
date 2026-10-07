"""Read-only node contract guards; no probe, ready writer or activation."""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from .adapter_base import InvalidMatcher
from . import provisioning_contract_rules as contract_rules
from .provisioning_identity import PPP, XRAY_MODES

adapter_version = contract_rules.adapter_version
validate_contract = contract_rules.validate_contract
_FIELDS = contract_rules.ENDPOINT_FIELDS

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
        contract = validate_contract(row.contract, backend)
    except (ValueError, TypeError, InvalidMatcher):
        raise HTTPException(409, "node_contract_not_ready") from None
    return contract
