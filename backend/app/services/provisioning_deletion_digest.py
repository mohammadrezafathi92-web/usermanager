"""Pure shared removal digest for parent ORM and child closed SQL snapshots.

Input is detached scalar mappings, never credentials for connecting to a
node. No ORM, database, transport, commit or activation. The Xray UUID is
an exact object identity and is hashed, never returned in a public result.
"""
import hashlib
import json

CONNECTION_FIELDS = ("id", "user_id", "purchase_id", "node_id", "type", "wg_peer_name", "wg_public_key",
    "wg_client_address", "ppp_username", "xr_email", "xr_uuid", "xr_flow")
NODE_FIELDS = ("type", "mt_wireguard_interface", "xr_panel_mode", "xr_inbound_tag", "xr_panel_inbound_id",
    "mt_host", "mt_port", "mt_use_ssl", "mt_api_ssl_port", "xr_panel_base_url", "xr_ssh_host", "xr_ssh_port",
    "se_host", "se_port", "se_hub_name")


def fingerprint(connection, node):
    values = [connection[name] for name in CONNECTION_FIELDS]
    values.extend(bool(node[name]) if name == "mt_use_ssl" and node[name] is not None else node[name]
        for name in NODE_FIELDS)
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
