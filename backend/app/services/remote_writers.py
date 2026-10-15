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

# Backend adapters (design section 9). They call writer methods on a client
# they are handed, and are themselves only ever run by a runner action that
# already holds the node gate - so, like the clients, they are not sites.
ADAPTER_MODULES = frozenset({
    "app.services.adapter_mikrotik_wg", "app.services.adapter_radius_ppp",
    "app.services.adapter_xray", "app.services.adapter_softether",
    "app.services.adapter_xray_panels",
    # The 5.10 contract probe: runs only as the CONTRACT_PROBE runner action,
    # under the node lock, on its own namespaced test identity.
    "app.services.provisioning_child_probe",
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
# Every legacy writer's remote part lives in ONE module of plain functions
# (Lifecycle 10.8): run inline under writer_gate by services/legacy_remote.py
# in off/shadow, and by the runner child in 'enforced'. Nothing else calls
# them (tests/test_remote_writers_inventory.py checks the importers).
_BLOCKS = "app.services.legacy_remote_blocks"
BLOCK_MODULE = _BLOCKS

register_writer_site(_BLOCKS, "wg_provision", (A.WG_ENSURE_PRESENT,),
                     {"ensure_wireguard_interface", "set_interface_address", "ensure_interface_address",
                      "add_peer", "upsert_simple_queue"})
register_writer_site(_BLOCKS, "xray_add", (A.XRAY_ENSURE_PRESENT,), {"add_client"})
register_writer_site(_BLOCKS, "softether_add", (A.SOFTETHER_ENSURE_PRESENT,), {"add_client"})
register_writer_site(_BLOCKS, "wg_remove", (A.WG_ENSURE_ABSENT,), {"remove_peer", "remove_simple_queue"})
register_writer_site(_BLOCKS, "xray_remove", (A.XRAY_ENSURE_ABSENT,), {"remove_client"})
register_writer_site(_BLOCKS, "softether_remove", (A.SOFTETHER_ENSURE_ABSENT,), {"remove_client"})
register_writer_site(_BLOCKS, "ppp_kick", (A.PPP_KICK_SESSION,), {"kick_ppp_session"})
register_writer_site(_BLOCKS, "wg_bounce", (A.WG_BOUNCE_PEER,), {"set_peer_disabled"})
register_writer_site(_BLOCKS, "xray_bounce", (A.XRAY_BOUNCE_CLIENT,), {"set_client_enabled"})
register_writer_site(_BLOCKS, "softether_bounce", (A.SOFTETHER_BOUNCE_USER,), {"set_client_enabled"})
register_writer_site(_BLOCKS, "wg_set_enabled", (A.WG_SET_PEER_ENABLED,), {"set_peer_disabled"})
register_writer_site(_BLOCKS, "xray_set_enabled", (A.XRAY_SET_CLIENT_ENABLED,), {"set_client_enabled"})
register_writer_site(_BLOCKS, "softether_set_enabled", (A.SOFTETHER_SET_USER_ENABLED,), {"set_client_enabled"})
register_writer_site(_BLOCKS, "wg_rename", (A.WG_RENAME_PEER,), {"rename_peer"})
register_writer_site(_BLOCKS, "wg_queue_sync", (A.WG_SET_SPEED_QUEUE,), {"upsert_simple_queue", "remove_simple_queue"})
register_writer_site(_BLOCKS, "mt_push", (A.MT_PUSH_RADIUS, A.MT_PUSH_PPTP, A.MT_PUSH_SSTP, A.MT_PUSH_L2TP,
                                          A.MT_PUSH_IKEV2),
                     {"push_radius_config", "push_pptp_config", "push_sstp_config", "push_l2tp_config",
                      "push_ikev2_config"})
register_writer_site(_BLOCKS, "xray_rebuild", (A.XRAY_REBUILD_CLIENTS,), {"add_client"})

# Action types that have no call site in today's code because the thing
# they do does not exist yet (the design adds it): the WireGuard address
# reconciliation job and the per-node contract probe.
ACTIONS_WITHOUT_LEGACY_SITE = frozenset({A.WG_RECONCILE_INTERFACE_ADDRESS, A.CONTRACT_PROBE})
