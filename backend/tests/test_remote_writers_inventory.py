"""Lifecycle/P6 batch L1a-1 - the remote-writer inventory, and proof that
this batch changed NOTHING about how the existing writers behave.

Design v7.1, section 10.9. Run:
    python3 backend/tests/test_remote_writers_inventory.py

  A. INVENTORY (LP-163, static half). services/remote_writers.py is
     compared with the application's source: every call of a remote writer
     method outside the client modules is inside a registered function,
     each registered function calls exactly the writer methods it declares,
     and nothing registered has disappeared.

  B. NOT WIRED. No existing module imports the gate, the runner, the DTO
     or the inventory; the runner has no real action registered. So the
     new code cannot be on any production path yet.

  C. CHARACTERIZATION. The remote call sequence of the existing writers,
     recorded with fake clients - the baseline the later batches (which DO
     route them through node_gate/the runner) must reproduce exactly. Here
     it also shows the gate was never entered: zero passages.
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import node_gate, quota_manager, remote_writers as rw, user_ops
from app.services.remote_action import ActionType
from app.services.remote_runner_registry import ACTIONS

failures: list[str] = []
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(BACKEND_DIR, "app")
NEW_MODULES = {"node_gate", "gate_locks", "remote_action", "remote_runner", "remote_runner_child",
               "remote_runner_registry", "remote_runner_selftest", "remote_writers"}


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def raises(exc_type, fn):
    try:
        fn()
    except exc_type:
        return True
    except Exception as exc:  # noqa: BLE001
        return f"raised {type(exc).__name__} instead"
    return "did not raise"


def app_modules():
    for root, _dirs, files in os.walk(APP_DIR):
        for name in sorted(files):
            if name.endswith(".py"):
                path = os.path.join(root, name)
                dotted = "app." + os.path.relpath(path, APP_DIR)[:-3].replace(os.sep, ".")
                yield dotted, path


def writer_calls(path):
    """{enclosing top-level function (or '<module>'): {writer method, ...}}
    for every `<something>.<writer method>(...)` call in the file."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    found: dict[str, set] = {}

    def visit(node, owner):
        for child in ast.iter_child_nodes(node):
            child_owner = owner
            if owner == "<module>" and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_owner = child.name
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in rw.WRITER_METHODS):
                found.setdefault(child_owner, set()).add(child.func.attr)
            visit(child, child_owner)

    visit(tree, "<module>")
    return found


# ===========================================================================
print("--- A. the inventory matches the code ---")
in_code: dict[tuple[str, str], set] = {}
client_internal: dict[str, set] = {}
for dotted, path in app_modules():
    calls = writer_calls(path)
    if dotted in rw.CLIENT_MODULES:
        for methods in calls.values():
            client_internal.setdefault(dotted, set()).update(methods)
        continue
    for function, methods in calls.items():
        in_code[(dotted, function)] = methods

registered = {site.key: set(site.writer_methods) for site in rw.writer_sites()}
check("every writer call site in the code is registered",
      sorted(set(in_code) - set(registered)), [])
check("every registered site still exists in the code",
      sorted(set(registered) - set(in_code)), [])
check("each site declares exactly the writer methods it calls",
      {key: (sorted(in_code[key]), sorted(registered[key])) for key in set(in_code) & set(registered)
       if in_code[key] != registered[key]},
      {})
check("thirteen writer functions today", len(registered), 13)
check("no writer call sits at module level", [k for k in in_code if k[1] == "<module>"], [])
check("every client module that exists is listed as a client module",
      sorted(rw.CLIENT_MODULES - {dotted for dotted, _ in app_modules()}), [])
check("writer calls inside client modules are the known internal ones only",
      {module: sorted(methods) for module, methods in sorted(client_internal.items())},
      {
          "app.services.hiddify_client": ["add_client"],
          "app.services.marzban_client": ["add_client"],
          "app.services.marzneshin_client": ["add_client"],
          "app.services.mikrotik_client": ["ensure_self_signed_certificate"],
          "app.services.softether_client": ["add_client"],
          "app.services.sui_client": ["add_client"],
          "app.services.threexui_client": ["add_client"],
          "app.services.xray_client": ["add_client", "remove_client", "restart_service", "write_config"],
      })
called_anywhere = set().union(*in_code.values()) | set().union(*client_internal.values())
check("the three MikroTik writer methods without any caller are exactly the declared ones",
      sorted(rw.WRITER_METHODS - called_anywhere), sorted(rw.UNCALLED_WRITER_METHODS))

print("--- A. every real action type is accounted for ---")
real_actions = {a for a in ActionType if not a.value.startswith("selftest_")}
check("each real action_type has a legacy site, or is one of the two that have none by design",
      sorted(a.value for a in real_actions - rw.action_types_in_use() - rw.ACTIONS_WITHOUT_LEGACY_SITE), [])
check("no action is both 'in use' and 'without a legacy site'",
      sorted(a.value for a in rw.action_types_in_use() & rw.ACTIONS_WITHOUT_LEGACY_SITE), [])
check("23 real action types, 4 self-test ones", (len(real_actions), len(set(ActionType) - real_actions)), (23, 4))

print("--- A. the registration API is type-safe ---")
E = rw.WriterRegistryError
check("a duplicate (module, function) is rejected",
      raises(E, lambda: rw.register_writer_site("app.services.user_ops", "provision_xray",
                                                (ActionType.XRAY_ENSURE_PRESENT,), {"add_client"})))
check("an action given as a plain string is rejected",
      raises(E, lambda: rw.register_writer_site("app.x", "f", ("wg_ensure_present",), {"add_peer"})))
check("no action types is rejected", raises(E, lambda: rw.register_writer_site("app.x", "f", (), {"add_peer"})))
check("a self-test action is not a writer site",
      raises(E, lambda: rw.register_writer_site("app.x", "f", (ActionType.SELFTEST_ECHO,), {"add_peer"})))
check("an unknown writer method is rejected",
      raises(E, lambda: rw.register_writer_site("app.x", "f", (ActionType.WG_ENSURE_PRESENT,), {"list_peers"})))
check("no writer methods is rejected",
      raises(E, lambda: rw.register_writer_site("app.x", "f", (ActionType.WG_ENSURE_PRESENT,), set())))
check("a module outside the application is rejected",
      raises(E, lambda: rw.register_writer_site("os", "system", (ActionType.WG_ENSURE_PRESENT,), {"add_peer"})))
check("a function that is not an identifier is rejected",
      raises(E, lambda: rw.register_writer_site("app.x", "f; rm", (ActionType.WG_ENSURE_PRESENT,), {"add_peer"})))
check("none of the rejected registrations left a trace", len(rw.writer_sites()), 13)
site = rw.site_for("app.services.user_ops", "provision_wireguard")
check("a site is immutable", raises(Exception, lambda: setattr(site, "function", "x")))

# ===========================================================================
print("--- B. nothing existing is routed through the new code ---")
importers = {}
for dotted, path in app_modules():
    short = dotted.split(".")[-1]
    if short in NEW_MODULES:
        continue
    tree = ast.parse(open(path, encoding="utf-8").read())
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            used |= {(node.module or "").split(".")[-1]} | {alias.name for alias in node.names}
        elif isinstance(node, ast.Import):
            used |= {alias.name.split(".")[-1] for alias in node.names}
    if used & NEW_MODULES:
        importers[dotted] = sorted(used & NEW_MODULES)
check("no pre-existing module imports the gate, the runner, the DTO or the inventory", importers, {})
check("the runner has no real action registered - it cannot reach a node",
      sorted(a.value for a in ACTIONS if not a.value.startswith("selftest_")), [])

# ===========================================================================
print("--- C. characterization of the existing writers (baseline for later batches) ---")


class Recorder:
    """A fake remote client: records every method call in order."""

    def __init__(self, log, returns=None):
        self._log, self._returns = log, returns or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self._log.append((name, args, tuple(sorted(kwargs.items()))))
            value = self._returns.get(name)
            return value(*args, **kwargs) if callable(value) else value
        return call


class FakeMikrotik:
    log: list = []
    returns: dict = {}

    @classmethod
    def for_node(cls, node):
        return Recorder(cls.log, cls.returns)


def install_fakes(log, mikrotik_returns=None, xray_returns=None, softether_returns=None):
    FakeMikrotik.log, FakeMikrotik.returns = log, mikrotik_returns or {}
    for module in (user_ops, quota_manager):
        module.MikrotikClient = FakeMikrotik
        module.client_for_node = lambda node: Recorder(log, xray_returns or {})
        module.softether_client_for_node = lambda node: Recorder(log, softether_returns or {})


def names(log):
    return [entry[0] for entry in log]


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
db.add(admin)
db.commit()
mt_node = models.Node(name="mt", type="mikrotik", enabled=True, mt_host="10.0.0.1",
                      mt_wireguard_interface="wg0", mt_client_subnet="10.66.66.0/24")
xr_node = models.Node(name="xr", type="xray", enabled=True, xr_inbound_tag="vless-in")
se_node = models.Node(name="se", type="softether", enabled=True, se_host="10.0.0.9", se_hub_name="VPN")
user = models.User(username="charuser", owner_admin_id=admin.id, total_quota_bytes=0, used_bytes=0)
db.add_all([mt_node, xr_node, se_node, user])
db.commit()
node_gate.counters.reset()

log: list = []
install_fakes(log, mikrotik_returns={"add_peer": "*1"})
wg = user_ops.provision_wireguard(db, user, mt_node)
check("provision_wireguard: interface, address, peer - in that order",
      names(log), ["ensure_wireguard_interface", "ensure_interface_address", "add_peer"])
add_peer = log[2]
check("provision_wireguard: add_peer(interface, public_key=, allowed_address=, comment=) with the stored identity",
      (add_peer[1], dict(add_peer[2])),
      (("wg0",), {"public_key": wg.wg_public_key, "allowed_address": wg.wg_client_address, "comment": wg.wg_peer_name}))
check("provision_wireguard: the row is committed only after the remote calls (today's order)",
      (wg.id is not None, wg.wg_client_address), (True, "10.66.66.2/32"))

log.clear()
wg_limited = user_ops.provision_wireguard(db, user, mt_node, speed_limit_mbps=20)
check("provision_wireguard with a speed limit: ... then the queue",
      names(log), ["ensure_wireguard_interface", "ensure_interface_address", "add_peer", "upsert_simple_queue"])
check("...queue named after the peer, for its address and limit",
      log[3][1], (user_ops.wg_speed_queue_name(wg_limited.wg_peer_name), wg_limited.wg_client_address, 20))

log.clear()
install_fakes(log, xray_returns={"add_client": "11111111-2222-3333-4444-555555555555"})
xr = user_ops.provision_xray(db, user, xr_node, flow="xtls-rprx-vision")
check("provision_xray: one add_client(tag, email, flow=) and the uuid the CLIENT returned is stored",
      (names(log), log[0][1], dict(log[0][2]), xr.xr_uuid),
      (["add_client"], ("vless-in", xr.xr_email), {"flow": "xtls-rprx-vision"}, "11111111-2222-3333-4444-555555555555"))

log.clear()
install_fakes(log)
se = user_ops.provision_softether(db, user, se_node)
check("provision_softether: one add_client(username, password) with the stored pair",
      (names(log), log[0][1]), (["add_client"], (se.ppp_username, se.ppp_password)))

log.clear()
install_fakes(log, mikrotik_returns={"list_peers": [{".id": "*7", "comment": wg.wg_peer_name}]})
user_ops.deprovision_connection(wg)
check("deprovision (wireguard, peer found by comment): list, remove by .id, remove queue",
      [(e[0], e[1]) for e in log],
      [("list_peers", ("wg0",)), ("remove_peer", ("*7",)),
       ("remove_simple_queue", (user_ops.wg_speed_queue_name(wg.wg_peer_name),))])

log.clear()
install_fakes(log, mikrotik_returns={"list_peers": []})
user_ops.deprovision_connection(wg)
check("deprovision (wireguard, peer NOT found): no remove_peer, the queue removal still runs, no error",
      names(log), ["list_peers", "remove_simple_queue"])

log.clear()
install_fakes(log)
user_ops.deprovision_connection(xr)
user_ops.deprovision_connection(se)
check("deprovision (xray, softether): remove_client by email+uuid / by username",
      [(e[0], e[1]) for e in log],
      [("remove_client", ("vless-in", xr.xr_email, xr.xr_uuid)), ("remove_client", (se.ppp_username,))])

log.clear()
install_fakes(log, mikrotik_returns={"list_peers": [{".id": "*7", "comment": wg.wg_peer_name}]})
wg.enabled = True
quota_manager._set_connection_enabled(db, wg, enabled=False)
check("quota disable (wireguard): list, set_peer_disabled(.id, disabled=True); flag follows",
      ([(e[0], e[1], dict(e[2])) for e in log], wg.enabled),
      ([("list_peers", ("wg0",), {}), ("set_peer_disabled", ("*7",), {"disabled": True})], False))

log.clear()
install_fakes(log, mikrotik_returns={"list_peers": []})
wg.enabled = True
quota_manager._set_connection_enabled(db, wg, enabled=False)
check("quota disable (wireguard, peer not found): nothing written, flag NOT changed",
      (names(log), wg.enabled), (["list_peers"], True))

log.clear()
install_fakes(log, xray_returns={"set_client_enabled": True}, softether_returns={"set_client_enabled": False})
xr.enabled, se.enabled = True, True
quota_manager._set_connection_enabled(db, xr, enabled=False)
quota_manager._set_connection_enabled(db, se, enabled=False)
check("quota disable (xray applied / softether not applied): one call each; flag only follows a confirmed change",
      ([(e[0], e[1]) for e in log], xr.enabled, se.enabled),
      ([("set_client_enabled", ("vless-in", xr.xr_email, xr.xr_uuid, "xtls-rprx-vision", False)),
        ("set_client_enabled", (se.ppp_username, se.ppp_password, False))], False, True))

log.clear()
quota_manager._set_connection_enabled(db, xr, enabled=False)
check("quota: already in the requested state -> no remote call at all", log, [])

check("none of the writers above went through node_gate (zero passages, zero locks counted)",
      node_gate.counters.snapshot(), {"mode_lock_miss": 0, "contention": 0, "passages": 0})
db.close()

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
