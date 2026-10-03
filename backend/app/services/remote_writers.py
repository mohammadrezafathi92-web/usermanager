"""The inventory of every remote WRITER in the application, as typed data
(Lifecycle/P6 design v7.1, section 10.9 - "mapping of every writer to an
action_type").

A "writer" is a call that changes something on a node: a MikroTik router,
an Xray host/panel, a SoftEther hub. Each one will, in a later batch, be
converted into a RemoteActionDTO executed by the runner under node_gate.
Until then this module is only a description - importing it changes
nothing - but it is a CHECKED description: tests/test_remote_writers_
inventory.py walks the application's source and fails if a writer call
exists that is not listed here, or if a listed one is gone. So the
inventory cannot silently drift from the code, and a new writer cannot be
added without declaring which action it belongs to.
"""
from __future__ import annotations

from dataclasses import dataclass

from .remote_action import ActionType

# Every method of a remote client that mutates the node. Reads (list_*,
# get_*, read_*, query_*, test_connection, check_reaches) are not here.
MIKROTIK_WRITER_METHODS = frozenset({
    "ensure_wireguard_interface", "ensure_interface_address", "set_interface_address",
    "add_peer", "remove_peer", "rename_peer", "set_peer_disabled",
    "upsert_simple_queue", "remove_simple_queue", "kick_ppp_session",
    "push_radius_config", "ensure_self_signed_certificate", "push_pptp_config",
    "push_sstp_config", "push_l2tp_config", "push_ikev2_config",
    "ensure_masquerade", "setup_socks_proxy", "disable_socks_proxy",
})
PANEL_WRITER_METHODS = frozenset({  # all six Xray clients and SoftEther share these names
    "add_client", "remove_client", "set_client_enabled", "write_config", "restart_service",
})
WRITER_METHODS = MIKROTIK_WRITER_METHODS | PANEL_WRITER_METHODS

# Client modules: writer calls INSIDE them are the clients' own internals
# (set_client_enabled -> add_client self-heal, add_client -> write_config
# -> restart_service, push_sstp_config -> ensure_self_signed_certificate).
# They run inside whichever action called the client and are not sites.
CLIENT_MODULES = frozenset({
    "app.services.mikrotik_client", "app.services.xray_client", "app.services.threexui_client",
    "app.services.softether_client", "app.services.marzban_client", "app.services.hiddify_client",
    "app.services.marzneshin_client", "app.services.sui_client",
})

# Writer methods that exist on MikrotikClient but have no caller today. They
# have no action_type; a future caller must register a site first.
UNCALLED_WRITER_METHODS = frozenset({"ensure_masquerade", "setup_socks_proxy", "disable_socks_proxy"})


class WriterRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class WriterSite:
    """One function of the application that calls remote writers."""
    module: str                        # dotted module, e.g. 'app.services.user_ops'
    function: str                      # function name inside that module
    action_types: tuple[ActionType, ...]
    writer_methods: frozenset[str]     # exactly the writer methods it calls

    def __post_init__(self):
        if not isinstance(self.module, str) or not self.module.startswith("app."):
            raise WriterRegistryError(f"module must be a dotted 'app.' path, got {self.module!r}")
        if not isinstance(self.function, str) or not self.function.isidentifier():
            raise WriterRegistryError(f"function must be an identifier, got {self.function!r}")
        if not self.action_types or not all(isinstance(a, ActionType) for a in self.action_types):
            raise WriterRegistryError("action_types must be a non-empty tuple of ActionType")
        if any(a.value.startswith("selftest_") for a in self.action_types):
            raise WriterRegistryError("self-test actions are not writer sites")
        methods = frozenset(self.writer_methods)
        unknown = methods - WRITER_METHODS
        if not methods or unknown:
            raise WriterRegistryError(f"writer_methods must be known writer methods; unknown: {sorted(unknown)}")
        object.__setattr__(self, "writer_methods", methods)
        object.__setattr__(self, "action_types", tuple(self.action_types))

    @property
    def key(self) -> tuple[str, str]:
        return (self.module, self.function)


_sites: dict[tuple[str, str], WriterSite] = {}


def register_writer_site(module: str, function: str, action_types, writer_methods) -> WriterSite:
    """The only way a site enters the inventory. Rejects a duplicate
    (module, function) and anything that is not well-typed."""
    site = WriterSite(module, function, tuple(action_types), frozenset(writer_methods))
    if site.key in _sites:
        raise WriterRegistryError(f"{module}.{function} is already registered")
    _sites[site.key] = site
    return site


def writer_sites() -> tuple[WriterSite, ...]:
    return tuple(_sites.values())


def site_for(module: str, function: str) -> WriterSite | None:
    return _sites.get((module, function))


def action_types_in_use() -> frozenset[ActionType]:
    return frozenset(a for site in _sites.values() for a in site.action_types)


A = ActionType
_USER_OPS, _QUOTA = "app.services.user_ops", "app.services.quota_manager"
_USERS, _NODES = "app.routers.users", "app.routers.nodes"

register_writer_site(_USER_OPS, "provision_wireguard", (A.WG_ENSURE_PRESENT,),
                     {"ensure_wireguard_interface", "set_interface_address", "ensure_interface_address",
                      "add_peer", "upsert_simple_queue"})
register_writer_site(_USER_OPS, "provision_xray", (A.XRAY_ENSURE_PRESENT,), {"add_client"})
register_writer_site(_USER_OPS, "provision_softether", (A.SOFTETHER_ENSURE_PRESENT,), {"add_client"})
register_writer_site(_USER_OPS, "deprovision_connection",
                     (A.WG_ENSURE_ABSENT, A.XRAY_ENSURE_ABSENT, A.SOFTETHER_ENSURE_ABSENT),
                     {"remove_peer", "remove_simple_queue", "remove_client"})
register_writer_site(_USER_OPS, "kick_connection",
                     (A.PPP_KICK_SESSION, A.WG_BOUNCE_PEER, A.XRAY_BOUNCE_CLIENT, A.SOFTETHER_BOUNCE_USER),
                     {"kick_ppp_session", "set_peer_disabled", "set_client_enabled"})
register_writer_site(_QUOTA, "_set_connection_enabled",
                     (A.WG_SET_PEER_ENABLED, A.XRAY_SET_CLIENT_ENABLED, A.SOFTETHER_SET_USER_ENABLED),
                     {"set_peer_disabled", "set_client_enabled"})
register_writer_site(_USERS, "update_connection", (A.WG_RENAME_PEER, A.WG_SET_SPEED_QUEUE),
                     {"rename_peer", "upsert_simple_queue", "remove_simple_queue"})
register_writer_site(_NODES, "push_radius_config", (A.MT_PUSH_RADIUS,), {"push_radius_config"})
register_writer_site(_NODES, "push_pptp_config", (A.MT_PUSH_PPTP,), {"push_radius_config", "push_pptp_config"})
register_writer_site(_NODES, "push_sstp_config", (A.MT_PUSH_SSTP,), {"push_radius_config", "push_sstp_config"})
register_writer_site(_NODES, "push_l2tp_config", (A.MT_PUSH_L2TP,), {"push_radius_config", "push_l2tp_config"})
register_writer_site(_NODES, "push_ikev2_config", (A.MT_PUSH_IKEV2,), {"push_radius_config", "push_ikev2_config"})
register_writer_site(_NODES, "rebuild_node_clients", (A.XRAY_REBUILD_CLIENTS,), {"add_client"})

# Action types that have no call site in today's code because the thing
# they do does not exist yet (the design adds it): the WireGuard address
# reconciliation job and the per-node contract probe.
ACTIONS_WITHOUT_LEGACY_SITE = frozenset({A.WG_RECONCILE_INTERFACE_ADDRESS, A.CONTRACT_PROBE})
