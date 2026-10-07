"""Lifecycle/P6 batches L1a-1 and L1a-2 - the remote-writer inventory, the
routing of every writer through writer_gate, and proof that doing so did not
change what the writers do.

Design v7.1, sections 10.2, 10.6, 10.9. Run:
    python3 backend/tests/test_remote_writers_inventory.py

  A. INVENTORY (LP-163, static half). services/remote_writers.py is
     compared with the application's source: every call of a remote writer
     method outside the client modules is inside a registered function,
     each registered function calls exactly the writer methods it declares,
     and nothing registered has disappeared.

  B. ROUTING. Every one of those writer calls is lexically inside a
     `with writer_gate(...)` block - a writer added later without one fails
     here. Nothing else of the new machinery is used by existing code: the
     runner, the DTO and the inventory are still imported by nobody, and
     the runner has no real action registered.

  C. CHARACTERIZATION (LP-133, LP-168). The remote call sequence of the
     existing writers, recorded with fake clients BEFORE they were routed
     through the gate, still holds exactly - same calls, same arguments,
     same order, same return values and error behaviour - now with one
     gate passage per remote block.

  D. LIVE SHAPE. With a verified schema and a usable lock directory, a
     legacy writer takes exactly one shared gate-mode lock per remote block
     and no node lock; two writers on one node still run concurrently; a
     broken gate does not break a writer.
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


def ungated_writer_calls(path):
    """Writer-method calls that are NOT lexically inside a `with` statement
    one of whose items is a call to writer_gate(...): [(line, method)]."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    loose = []

    def is_gate(item):
        call = item.context_expr
        return (isinstance(call, ast.Call)
                and ((isinstance(call.func, ast.Name) and call.func.id == "writer_gate")
                     or (isinstance(call.func, ast.Attribute) and call.func.attr == "writer_gate")))

    def visit(node, gated):
        for child in ast.iter_child_nodes(node):
            child_gated = gated
            if isinstance(child, (ast.With, ast.AsyncWith)) and any(is_gate(i) for i in child.items):
                child_gated = True
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                child_gated = False     # a nested function runs later, outside the with
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr in rw.WRITER_METHODS and not gated):
                loose.append((child.lineno, child.func.attr))
            visit(child, child_gated)

    visit(tree, False)
    return loose


def imports_adapter(path) -> bool:
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.ImportFrom):
            names = [(node.module or "").split(".")[-1]] + [alias.name for alias in node.names]
        elif isinstance(node, ast.Import):
            names = [alias.name.split(".")[-1] for alias in node.names]
        else:
            continue
        if any(name.startswith("adapter_") and name != "adapter_base" for name in names):
            return True
    return False


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
    if dotted in rw.ADAPTER_MODULES:
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
check("every adapter exists; only the fenced cancellation worker integrates one",
      (sorted(rw.ADAPTER_MODULES - {dotted for dotted, _ in app_modules()}),
       sorted(dotted for dotted, path in app_modules()
              if dotted not in rw.ADAPTER_MODULES and imports_adapter(path))),
      ([], ["app.services.reseller_refund_worker"]))
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
print("--- B. every writer goes through writer_gate; nothing else of the new code is used ---")
loose = {}
for dotted, path in app_modules():
    if dotted in rw.CLIENT_MODULES or dotted in rw.ADAPTER_MODULES:
        continue
    found = ungated_writer_calls(path)
    if found:
        loose[dotted] = found
check("no writer call outside a `with writer_gate(...)` block", loose, {})

snippet = "def f(node):\n    with writer_gate(node), C.for_node(node) as mt:\n        mt.add_peer(1)\n    mt.remove_peer(2)\n"
probe_path = os.path.join(BACKEND_DIR, "tests", "_gate_probe_tmp.py")
with open(probe_path, "w", encoding="utf-8") as fh:
    fh.write(snippet)
try:
    check("the detector itself: a call inside the block passes, one after it is caught",
          ungated_writer_calls(probe_path), [(4, "remove_peer")])
finally:
    os.remove(probe_path)

importers = {}
for dotted, path in app_modules():
    short = dotted.split(".")[-1]
    if short in NEW_MODULES:
        continue
    tree = ast.parse(open(path, encoding="utf-8").read())
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = (node.module or "").split(".")[-1]
            if module in NEW_MODULES:
                used |= {f"{module}.{alias.name}" for alias in node.names}
            used |= {alias.name for alias in node.names if alias.name in NEW_MODULES}
        elif isinstance(node, ast.Import):
            used |= {alias.name.split(".")[-1] for alias in node.names if alias.name.split(".")[-1] in NEW_MODULES}
    if used:
        importers[dotted] = sorted(used)
check("legacy writers are gated; private ownership self-tests use lock primitives; no live runner actions",
      importers,
      {"app.routers.nodes": ["node_gate.writer_gate"], "app.routers.users": ["node_gate.writer_gate"],
       "app.services.quota_manager": ["node_gate.writer_gate"], "app.services.user_ops": ["node_gate.writer_gate"],
       "app.services.reseller_refund_fence": ["gate_locks.FileLock", "gate_locks.lock_base_dir"],
       "app.services.provisioning_lock_verification": ["gate_locks"],
       "app.services.provisioning_ownership": ["gate_locks"],
       "app.services.provisioning_installation": ["gate_locks"],
       "app.routers.provisioning": ["remote_action.ActionType", "remote_runner_registry.ACTIONS"]})
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

check("each of the 12 remote blocks above made exactly one gate passage; the gate never failed or refused",
      node_gate.counters.snapshot(),
      {"mode_lock_miss": 12, "contention": 0, "passages": 12, "ungated": 0, "gate_error": 0})

print("--- C. error behaviour is unchanged ---")
from fastapi import HTTPException  # noqa: E402

from app.services.mikrotik_client import MikrotikError  # noqa: E402
from app.services.xray_client import XrayError  # noqa: E402


def http_status(fn):
    try:
        fn()
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return None


def failing(exc):
    def call(*a, **k):
        raise exc
    return call


log.clear()
install_fakes(log, mikrotik_returns={"add_peer": failing(MikrotikError("router said no"))})
before = db.query(models.Connection).count()
check("provision_wireguard: a router error is still HTTP 400 with the router's message",
      http_status(lambda: user_ops.provision_wireguard(db, user, mt_node)), (400, "router said no"))
check("...and still leaves no Connection row", db.query(models.Connection).count(), before)
install_fakes(log, xray_returns={"add_client": failing(XrayError("panel down"))})
check("provision_xray: a panel error is still HTTP 400",
      http_status(lambda: user_ops.provision_xray(db, user, xr_node)), (400, "panel down"))
install_fakes(log, mikrotik_returns={"list_peers": failing(RuntimeError("unexpected"))})
try:
    user_ops.deprovision_connection(wg)
    unexpected = None
except RuntimeError as exc:
    unexpected = str(exc)
check("an unexpected exception type still propagates as itself (the gate neither hides nor rewraps it)",
      unexpected, "unexpected")

# ===========================================================================
print("--- D. live shape: verified schema + usable lock directory ---")
import fcntl  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402

from app.services import gate_locks, provisioning_schema as ps  # noqa: E402

os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-writers-locks-")
live_engine = create_engine(f"sqlite:///{os.path.join(tempfile.mkdtemp(prefix='um-writers-db-'), 'live.db')}",
                            connect_args={"check_same_thread": False})
LiveSession = sessionmaker(bind=live_engine)
check("provisioning schema bootstraps", ps.bootstrap(live_engine, LiveSession)["ready"], True)
real_read = node_gate.read_runtime
node_gate.read_runtime = lambda factory=None: real_read(LiveSession)
node_gate.reset_cache()
node_gate.counters.reset()

flocks = []
real_flock = fcntl.flock


def spy_flock(fd, operation):
    try:
        name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
    except OSError:
        name = os.path.basename(fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024).split(b"\0")[0].decode())
    flocks.append((name, "SH" if operation & fcntl.LOCK_SH else "EX"))
    return real_flock(fd, operation)


gate_locks.fcntl.flock = spy_flock
log.clear()
install_fakes(log, mikrotik_returns={"add_peer": "*1"})
live_wg = user_ops.provision_wireguard(db, user, mt_node, speed_limit_mbps=5)
gate_locks.fcntl.flock = real_flock
check("a legacy writer takes exactly ONE lock: the shared gate-mode lock; no node lock, nothing exclusive",
      flocks, [("gate-mode.lock", "SH")])
check("...and makes the same remote calls as before",
      names(log), ["ensure_wireguard_interface", "ensure_interface_address", "add_peer", "upsert_simple_queue"])
check("...one passage, no miss", node_gate.counters.snapshot(),
      {"mode_lock_miss": 0, "contention": 0, "passages": 1, "ungated": 0, "gate_error": 0})
check("no node lock file exists", [f for f in os.listdir(os.path.dirname(gate_locks.mode_lock_path(
    LiveSession().get(__import__("app.models_provisioning", fromlist=["x"]).ProvisioningRuntimeState, 1).installation_uuid)))
    if f.startswith("node-")], [])

barrier = threading.Barrier(2, timeout=5)
together = []


class Meeting:
    """A fake client whose remote call waits until BOTH writers are inside."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_peers(self, interface):
        try:
            barrier.wait()
            together.append(True)
        except threading.BrokenBarrierError:
            together.append(False)
        return []

    def remove_simple_queue(self, name):
        return None


class MeetingFactory:
    @classmethod
    def for_node(cls, node):
        return Meeting()


user_ops.MikrotikClient = MeetingFactory
threads = [threading.Thread(target=user_ops.deprovision_connection, args=(live_wg,)) for _ in range(2)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
check("two legacy writers on the SAME node are inside their remote blocks at the same time (no serialization)",
      together, [True, True])

real_node_gate = node_gate.node_gate


def broken_gate(node_id, **kw):
    raise RuntimeError("gate is broken")


node_gate.node_gate = broken_gate
log.clear()
install_fakes(log, mikrotik_returns={"list_peers": []})
node_gate.counters.reset()
user_ops.deprovision_connection(live_wg)
node_gate.node_gate = real_node_gate
check("a broken gate does not stop a legacy writer: the remote calls still happen",
      names(log), ["list_peers", "remove_simple_queue"])
check("...and it is counted", node_gate.counters.snapshot()["gate_error"], 1)
node_gate.read_runtime = real_read
db.close()

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
