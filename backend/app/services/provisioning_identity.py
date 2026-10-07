"""Pure immutable removal-identity comparison; no queries or remote calls."""
from .. import models

PPP = frozenset(("openvpn", "l2tp", "ikev2", "sstp", "pptp"))
XRAY_MODES = {"xray_ssh": "ssh", "threexui": "3xui", "marzban": "marzban",
              "hiddify": "hiddify", "marzneshin": "marzneshin", "sui": "sui"}


def removal_matches(step, connection, node, *, removed=False):
    if (step.node_id, step.protocol) != (connection.node_id, connection.type.value):
        return False
    if step.backend == "mikrotik_wg":
        source = (node.mt_wireguard_interface, connection.wg_peer_name,
                  connection.wg_public_key, connection.wg_client_address)
        return node.type == models.NodeType.mikrotik and step.protocol == "wireguard" and all(source) and source == (
            step.wg_interface, step.wg_peer_name, step.wg_public_key, step.wg_client_address)
    if step.backend in ("radius_ppp", "softether"):
        valid = (node.type == models.NodeType.mikrotik and step.protocol in PPP) if step.backend == "radius_ppp" else (
            node.type == models.NodeType.softether and step.protocol == "softether")
        return valid and bool(connection.ppp_username) and step.account_username == connection.ppp_username
    if step.backend in XRAY_MODES:
        return node.type == models.NodeType.xray and step.protocol == "xray" and (
            node.xr_panel_mode or "ssh") == XRAY_MODES[step.backend] and bool(connection.xr_email) and (
            step.xr_email, step.xr_inbound_tag, step.xr_panel_inbound_id) == (
            connection.xr_email, node.xr_inbound_tag, node.xr_panel_inbound_id) and (
            removed or (bool(connection.xr_uuid) and step.staged_xr_uuid == connection.xr_uuid))
    return False
