"""Shared node-contract rules, safe to import in the read-only child."""
import hashlib
import json
from pathlib import Path

from sqlalchemy import text

from .adapter_base import InvalidMatcher, validate_matchers

# A contract records the version of the code that verified it. The version is
# PER BACKEND: the shared contract code plus that backend's own adapter, so a
# change to one adapter (e.g. WireGuard) never invalidates another backend's
# contracts (receipt 1140). Cancellation/refund helpers live outside every
# hashed file (adapter_mikrotik_wg_refund.py). tests/
# test_provisioning_contract_version_scope.py pins the current versions.
SHARED_CONTRACT_FILES = ("adapter_base.py", "provisioning_contracts.py", "provisioning_contract_rules.py")
BACKEND_ADAPTER_FILES = {
    "mikrotik_wg": "adapter_mikrotik_wg.py", "softether": "adapter_softether.py",
    "xray_ssh": "adapter_xray.py", "threexui": "adapter_xray.py",
    "marzban": "adapter_xray_panels.py", "hiddify": "adapter_xray_panels.py",
    "marzneshin": "adapter_xray_panels.py", "sui": "adapter_xray_panels.py",
}
ENDPOINT_FIELDS = ("type", "xr_panel_mode", "mt_host", "mt_port", "mt_use_ssl", "mt_api_ssl_port",
    "mt_wireguard_interface", "xr_ssh_host", "xr_ssh_port", "xr_panel_base_url", "se_host", "se_port",
    "se_hub_name", "xr_inbound_tag", "xr_panel_inbound_id", "xr_config_path", "xr_service_name")


def contract_files(backend):
    if backend not in BACKEND_ADAPTER_FILES:
        raise ValueError(f"no node contract for backend {backend!r}")
    return SHARED_CONTRACT_FILES + (BACKEND_ADAPTER_FILES[backend],)


def adapter_version(backend, root=None):
    """The contract-code version of ONE backend (40 hex). `root` is only for
    the scope test's scratch copy; production always reads this package."""
    base = Path(root) if root is not None else Path(__file__).parent
    return hashlib.sha1(b"".join((base / name).read_bytes() for name in contract_files(backend))).hexdigest()


def validate_contract(raw, backend):
    contract = json.loads(raw)
    if not isinstance(contract, dict) or "not_exist" not in contract:
        raise ValueError("node_contract_invalid")
    matchers = validate_matchers(contract["not_exist"])
    for matcher in matchers:
        if matcher["kind"] == "http" and (backend not in (
                "threexui", "marzban", "hiddify", "marzneshin", "sui") or matcher["json_path"] not in (
                "detail", "message", "msg", "error", "error.message", "error.code") or
                type(matcher["equals"]) not in (str, int) or not 100 <= matcher["status"] <= 599):
            raise ValueError("node_contract_invalid")
        if matcher["kind"] == "jsonrpc" and backend != "softether":
            raise ValueError("node_contract_invalid")
    return contract


def contract_statement():
    fields = ", ".join("n." + field + " AS n_" + field for field in ENDPOINT_FIELDS)
    return text("SELECT c.state, c.adapter_version, c.server_fingerprint, c.config_fingerprint, "
        "c.contract, c.version, " + fields + " FROM provisioning_node_contracts c "
        "JOIN nodes n ON n.id=c.node_id WHERE c.node_id=:node_id AND c.backend=:backend")


def node_endpoint_statement():
    """The legacy runner's read of one node's public endpoint (no secrets)."""
    fields = ", ".join("n." + field + " AS n_" + field for field in ENDPOINT_FIELDS)
    return text("SELECT n.id AS n_id, " + fields + " FROM nodes n WHERE n.id=:node_id")


def endpoint_fingerprint(row):
    values = {field: row["n_" + field] for field in ENDPOINT_FIELDS}
    if values["mt_use_ssl"] is not None:
        values["mt_use_ssl"] = bool(values["mt_use_ssl"])
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
