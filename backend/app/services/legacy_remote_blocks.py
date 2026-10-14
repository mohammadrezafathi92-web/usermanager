"""The remote part of every legacy writer, as plain functions (Lifecycle 10.8).

Each function takes a connected client and plain values and does exactly
the client calls the legacy site always did - same order, same arguments -
and returns a plain JSON value. They do not read the database, take a lock,
log a customer's identity or build a client: the caller does that.

  gate off / shadow  services/legacy_remote.py runs the function inline,
                     inside writer_gate, with the legacy client - today.
  gate enforced      the same function runs in the runner child
                     (provisioning_child_legacy) with the guarded child
                     client, under the real node lock and ownership check.

So semantics are identical in every mode; only where the code runs changes.
BLOCKS is the closed name -> function table the child accepts.
"""
from __future__ import annotations

from typing import Optional


def _peer(mt, interface: str, peer_name: str):
    peers = mt.list_peers(interface)
    return next((p for p in peers if p.get("comment") == peer_name), None)


# ------------------------------------------------------------------ provisioning (user_ops)
def wg_provision(mt, *, interface: str, endpoint_port: int, subnet_expanded: bool, gateway_with_prefix: str,
                 public_key: str, client_address: str, peer_name: str, queue_name: str,
                 speed_limit_mbps: Optional[int]) -> None:
    mt.ensure_wireguard_interface(interface, endpoint_port)
    if subnet_expanded:
        mt.set_interface_address(interface, gateway_with_prefix)
    else:
        mt.ensure_interface_address(interface, gateway_with_prefix)
    mt.add_peer(interface, public_key=public_key, allowed_address=client_address, comment=peer_name)
    if speed_limit_mbps:
        mt.upsert_simple_queue(queue_name, client_address, speed_limit_mbps)
    return None


def xray_add(xc, *, inbound_tag, email: str, flow: str = ""):
    return xc.add_client(inbound_tag, email, flow=flow or "")


def softether_add(sc, *, username: str, password: str) -> None:
    sc.add_client(username, password)
    return None


# ------------------------------------------------------------------ deprovision (user_ops)
def wg_remove(mt, *, interface: str, peer_name: str, queue_name: str) -> dict:
    match = _peer(mt, interface, peer_name)
    if match:
        mt.remove_peer(match[".id"])
    mt.remove_simple_queue(queue_name)
    return {"found": bool(match)}


def xray_remove(xc, *, inbound_tag, email: str, client_uuid: Optional[str]) -> None:
    xc.remove_client(inbound_tag, email, client_uuid)
    return None


def softether_remove(sc, *, username: str) -> None:
    sc.remove_client(username)
    return None


# ------------------------------------------------------------------ kick (user_ops)
def ppp_kick(mt, *, username: str) -> bool:
    return bool(mt.kick_ppp_session(username))


def wg_bounce(mt, *, interface: str, peer_name: str) -> bool:
    match = _peer(mt, interface, peer_name)
    if not match:
        return False
    mt.set_peer_disabled(match[".id"], disabled=True)
    mt.set_peer_disabled(match[".id"], disabled=False)
    return True


def xray_bounce(xc, *, inbound_tag, email: str, client_uuid: Optional[str], flow: str = "") -> None:
    xc.set_client_enabled(inbound_tag, email, client_uuid, flow or "", False)
    xc.set_client_enabled(inbound_tag, email, client_uuid, flow or "", True)
    return None


def softether_bounce(sc, *, username: str, password: Optional[str]) -> None:
    sc.set_client_enabled(username, password, False)
    sc.set_client_enabled(username, password, True)
    return None


# ------------------------------------------------------------------ enable/disable (quota_manager)
def wg_set_enabled(mt, *, interface: str, peer_name: str, enabled: bool) -> bool:
    match = _peer(mt, interface, peer_name)
    if not match:
        return False
    mt.set_peer_disabled(match[".id"], disabled=not enabled)
    return True


def xray_set_enabled(xc, *, inbound_tag, email: str, client_uuid: Optional[str], flow: str, enabled: bool) -> bool:
    return bool(xc.set_client_enabled(inbound_tag, email, client_uuid, flow or "", enabled))


def softether_set_enabled(sc, *, username: str, password: Optional[str], enabled: bool) -> bool:
    return bool(sc.set_client_enabled(username, password, enabled))


# ------------------------------------------------------------------ connection edit (routers/users)
def wg_rename(mt, *, interface: str, old_name: str, new_name: str) -> None:
    mt.rename_peer(interface, old_name, new_name)
    return None


def wg_queue_sync(mt, *, renamed: bool, old_queue: str, new_queue: str, address: Optional[str],
                  limit: Optional[int]) -> None:
    if renamed:
        mt.remove_simple_queue(old_queue)
    if limit:
        mt.upsert_simple_queue(new_queue, address, limit)
    else:
        mt.remove_simple_queue(new_queue)
    return None


# ------------------------------------------------------------------ node configuration (routers/nodes)
PUSH_CALLS = frozenset(("push_radius_config", "push_pptp_config", "push_sstp_config",
                        "push_l2tp_config", "push_ikev2_config"))


def mt_push(mt, *, calls: list) -> list:
    """`calls`: [[method, kwargs], ...] restricted to the five push methods.
    Returns each call's return value (e.g. the SSTP certificate name)."""
    results = []
    for method, kwargs in calls:
        if method not in PUSH_CALLS or not isinstance(kwargs, dict):
            raise ValueError("push call not allowed")
        if method == "push_radius_config":
            results.append(mt.push_radius_config(**kwargs))
        elif method == "push_pptp_config":
            results.append(mt.push_pptp_config(**kwargs))
        elif method == "push_sstp_config":
            results.append(mt.push_sstp_config(**kwargs))
        elif method == "push_l2tp_config":
            results.append(mt.push_l2tp_config(**kwargs))
        else:
            results.append(mt.push_ikev2_config(**kwargs))
    return results


def xray_rebuild(xc, *, inbound_tag, clients: list) -> dict:
    """`clients`: [{"name", "email", "client_uuid", "flow"}]; same skips as the route."""
    existing = ({c.get("email") for c in xc.list_clients_with_usage()}
                if hasattr(xc, "list_clients_with_usage") else set())
    imported, skipped = [], []
    for item in clients:
        email, client_uuid = item.get("email"), item.get("client_uuid")
        if not email or not client_uuid:
            skipped.append([item.get("name"), "شناسه/ایمیل ذخیره‌شده ندارد"])
            continue
        if email in existing:
            skipped.append([email, "از قبل روی نود هست"])
            continue
        try:
            xc.add_client(inbound_tag, email, client_uuid=client_uuid, flow=item.get("flow") or "")
            imported.append(email)
        except Exception as exc:  # noqa: BLE001 - the route reports per-client failures the same way
            if type(exc).__name__ != "XrayError":
                raise
            skipped.append([email, str(exc)])
    return {"imported": imported, "skipped": skipped}


# ------------------------------------------------------------------ WireGuard address reconciliation (10.5)
def wg_reconcile(mt, *, interface: str, desired_subnet: str) -> dict:
    """The 9.1 interface-address contract toward `desired_subnet`: add or widen
    only, never narrow. In the runner `desired_subnet` is replaced by the
    value read from the database AFTER the node lock was taken."""
    from .adapter_base import AdapterPermanentError
    from .adapter_mikrotik_wg import ADDRESS_ADDED, ensure_interface_address_exact_or_wider
    try:
        outcome = ensure_interface_address_exact_or_wider(mt, interface, desired_subnet)
        result = "repaired" if outcome == ADDRESS_ADDED else outcome
    except AdapterPermanentError:
        result = "conflict"
    rows = mt.list_interface_addresses(interface)
    return {"result": result, "router_address": rows[0].get("address") if len(rows) == 1 else None,
            "db_subnet": desired_subnet}


BLOCKS = {name: function for name, function in globals().items()
          if callable(function) and not name.startswith("_") and name != "Optional"}

# Which client each block needs, so the child builds the right one.
CLIENT_KIND = {
    "wg_provision": "mikrotik", "wg_remove": "mikrotik", "ppp_kick": "mikrotik", "wg_bounce": "mikrotik",
    "wg_set_enabled": "mikrotik", "wg_rename": "mikrotik", "wg_queue_sync": "mikrotik", "mt_push": "mikrotik",
    "xray_add": "xray", "xray_remove": "xray", "xray_bounce": "xray", "xray_set_enabled": "xray",
    "xray_rebuild": "xray",
    "softether_add": "softether", "softether_remove": "softether", "softether_bounce": "softether",
    "softether_set_enabled": "softether", "wg_reconcile": "mikrotik",
}
assert set(CLIENT_KIND) == set(BLOCKS), "every block declares its client kind"

# Arguments that are customer credentials: sent in the DTO's secret field.
# (push calls carry the RADIUS secret, the IPsec secret and the IKEv2 PSK.)
CREDENTIAL_ARGUMENTS = frozenset(("password", "client_uuid", "clients", "calls"))
