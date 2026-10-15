"""Runtime write guard of the legacy remote clients (Lifecycle 10.7).

Each of the eight legacy clients is driven through its REAL lowest layer
(RouterOS API object, SSH channel/SFTP, requests Session, SoftEther JSON-RPC)
against a fake endpoint that records what would have left the process:

  off       writes and reads pass; nothing counted          (zero behaviour change)
  shadow    writes pass; a write outside node_gate for that node is counted
            as writer_not_instrumented (per node); inside node_gate it is not
  enforced  every write outside the runner is refused BEFORE a single byte
            is sent; reads still pass; a client without a saved node and a
            context of another node are refused too
  nested    a client method that calls further writers stays inside one context
  stale     a cached 'off' never lets an uninstrumented write through once the
            database says 'enforced'; node_gate refreshes a stale cached mode
            as soon as the gate-mode state file names another epoch
"""
import os
import sys
import tempfile
import uuid

DIRECTORY = tempfile.mkdtemp(prefix="um-transport-guard-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

import requests
from requests.adapters import BaseAdapter

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import gate_locks, node_gate, provisioning_schema, transport_guard
from app.services.hiddify_client import HiddifyClient
from app.services.marzban_client import MarzbanClient
from app.services.marzneshin_client import MarzneshinClient
from app.services.mikrotik_client import MikrotikClient
from app.services.softether_client import SoftEtherClient, client_for_node as softether_for_node
from app.services.sui_client import SuiClient
from app.services.threexui_client import ThreeXUIClient
from app.services.xray_client import XrayClient, client_for_node

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
with SessionLocal() as db:
    db.add_all([models.Node(id=11, name="mt", type=models.NodeType.mikrotik, mt_host="10.1.1.1"),
                models.Node(id=12, name="se", type=models.NodeType.softether, se_host="10.1.1.2", se_hub_name="H"),
                models.Node(id=13, name="xs", type=models.NodeType.xray, xr_panel_mode="ssh", xr_ssh_host="10.1.1.3")]
               + [models.Node(id=20 + i, name=f"xp{i}", type=models.NodeType.xray, xr_panel_mode=mode,
                              xr_panel_base_url=f"http://10.2.0.{i}:2053/base", xr_panel_inbound_id=1,
                              xr_panel_api_token="tok", xr_panel_username="u", xr_panel_password="p")
                  for i, mode in enumerate(("3xui", "marzban", "hiddify", "marzneshin", "sui"))])
    db.commit()
    INSTALLATION = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    nodes = {node.id: node for node in db.query(models.Node)}
    db.expunge_all()


def set_mode(mode):
    """The persisted mode and its state file, exactly as a real change leaves them."""
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        epoch = row.gate_mode_epoch + 1
        row.gate_mode, row.gate_mode_epoch = mode, epoch
        if mode != "off":
            row.lock_backend, row.lock_verified_at = "flock", row.changed_at
            if mode == "enforced":
                row.owner_state, row.owner_host_id, row.owner_boot_id = "active", "e" * 64, str(uuid.uuid4())
                row.owner_claimed_at = row.owner_heartbeat_at = row.changed_at
        db.commit()
    gate_locks.write_mode_state(INSTALLATION, str(epoch))


def not_instrumented():
    node_gate.flush_counters()
    with SessionLocal() as db:
        return {row.node_id: row.writer_not_instrumented_count for row in db.query(mp.ProvisioningGateStat)
                if row.writer_not_instrumented_count}


# ------------------------------------------------------------------ fakes of the lowest layer
class FakeRouteros:
    def __init__(self):
        self.sent = []

    def __call__(self, command, **kwargs):
        self.sent.append(command)
        yield {"ret": "*1"} if command.endswith("/add") else {".id": "*1", "name": "wg0"}

    def close(self):
        pass


class FakeChannel:
    def recv_exit_status(self):
        return 0


class FakeStream:
    channel = FakeChannel()

    def read(self):
        return b""


class FakeSsh:
    def __init__(self):
        self.sent = []

    def exec_command(self, command, timeout=None):
        self.sent.append(command)
        return None, FakeStream(), FakeStream()

    def open_sftp(self):
        owner = self

        class Sftp:
            def open(self, path, mode="r", bufsize=-1):
                owner.sent.append(f"open {mode} {path}")

                class File:
                    def write(self, data):
                        owner.sent.append("write")

                    def read(self):
                        return b"{}"

                    def close(self):
                        pass

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        pass
                return File()

            def close(self):
                pass
        return Sftp()

    def close(self):
        pass


class FakeAdapter(BaseAdapter):
    def __init__(self):
        super().__init__()
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(f"{request.method} {request.url}")
        response = requests.Response()
        response.status_code, response._content = 200, b'{"result": {}, "success": true, "obj": []}'
        response.headers["Content-Type"] = "application/json"
        response.url, response.request = request.url, request
        return response

    def close(self):
        pass


def mikrotik():
    client = MikrotikClient.for_node(nodes[11])
    raw = FakeRouteros()
    client._api = transport_guard.RouterosApi(raw, client)
    read = lambda: list(client._api.path("interface", "wireguard"))
    write = lambda: client._api.path("interface", "wireguard", "peers").add(comment="x")
    return client, raw, read, write


def xray_ssh():
    client = client_for_node(nodes[13])
    raw = FakeSsh()
    client._client = transport_guard.SshTransport(raw, client)
    read = lambda: client._client.exec_command("test -f /usr/local/etc/xray/config.json && echo __FOUND__")
    def write():
        with client._client.open_sftp().open("/usr/local/etc/xray/config.json.tmp", "w") as handle:
            handle.write(b"{}")
    return client, raw, read, write


def panel(node_id):
    client = client_for_node(nodes[node_id])
    if client.session is None:                    # S-UI builds its Session in connect()
        original = SuiClient.test_connection
        SuiClient.test_connection = lambda self: True
        try:
            client.connect()
        finally:
            SuiClient.test_connection = original
    adapter = FakeAdapter()
    client.session.mount("http://", adapter)
    base = client.base_url.rstrip("/")
    read = lambda: client.session.get(base + "/api/users")
    write = lambda: client.session.post(base + "/api/user", json={"username": "x"})
    return client, adapter, read, write


def softether():
    client = softether_for_node(nodes[12])
    adapter = FakeAdapter()
    client.session.mount("https://", adapter)
    client.session.mount("http://", adapter)
    read = lambda: client._call("EnumUser", {"HubName_str": "H"})
    write = lambda: client._call("CreateUser", {"HubName_str": "H"})
    return client, adapter, read, write


CLIENTS = {"mikrotik": (11, mikrotik), "xray_ssh": (13, xray_ssh), "softether": (12, softether)}
CLIENTS.update({name: (20 + i, (lambda nid: (lambda: panel(nid)))(20 + i))
                for i, name in enumerate(("threexui", "marzban", "hiddify", "marzneshin", "sui"))})


def attempt(call):
    try:
        call()
        return "sent"
    except transport_guard.GateViolation:
        return "refused"
    except Exception as exc:  # noqa: BLE001 - a client-level parse error after a real send still counts as sent
        return f"sent ({type(exc).__name__})"


def normalized(value):
    return "sent" if value.startswith("sent") else value


check("every legacy factory binds its client to the saved node",
      {name: transport_guard._node(build()[0]) for name, (_node, build) in CLIENTS.items()},
      {name: node for name, (node, _build) in CLIENTS.items()})
check("the five panels and SoftEther send through a guarded Session",
      sorted(type(build()[0].session).__name__ for name, (_n, build) in CLIENTS.items() if name not in ("mikrotik", "xray_ssh")),
      ["GuardedSession"] * 5 + ["Session"])
check("SoftEther guards in _call (its session stays a plain Session)", type(softether()[0].session).__name__, "Session")
# A new Xray panel mode added to client_for_node without a guard must fail here.
from app.services.provisioning_identity import XRAY_MODES
import inspect
guarded = {}
for backend, mode in XRAY_MODES.items():
    probe = models.Node(id=99, name="probe", type=models.NodeType.xray, xr_panel_mode=mode,
                        xr_panel_base_url="http://10.3.0.1/x", xr_panel_inbound_id=1, xr_panel_api_token="t",
                        xr_panel_username="u", xr_panel_password="p", xr_ssh_host="10.3.0.2")
    built = client_for_node(probe)
    guarded[mode] = ("SshTransport" in inspect.getsource(type(built).connect)) if backend == "xray_ssh" else (
        isinstance(built.session, transport_guard.GuardedSession) or "GuardedSession" in inspect.getsource(type(built).connect))
check("every client class client_for_node can return has the write guard", guarded, {mode: True for mode in XRAY_MODES.values()})
check("MikroTik and SoftEther guard at their lowest layer",
      ("RouterosApi" in inspect.getsource(MikrotikClient.connect), "softether_call" in inspect.getsource(SoftEtherClient._call)),
      (True, True))

# ------------------------------------------------------------------ off
node_gate.reset_cache()
for name, (node_id, build) in CLIENTS.items():
    client, sink, read, write = build()
    results = (normalized(attempt(read)), normalized(attempt(write)))
    check(f"off: {name} reads and writes go through", (results, len(sink.sent) >= 2), (("sent", "sent"), True))
check("off: nothing is counted", not_instrumented(), {})

# ------------------------------------------------------------------ shadow
set_mode("shadow")
node_gate.reset_cache()
for name, (node_id, build) in CLIENTS.items():
    client, sink, read, write = build()
    check(f"shadow: {name} uninstrumented write still goes through", normalized(attempt(write)), "sent")
# (Xray SSH: opening the config for writing and the write itself are two guarded operations.)
EXPECTED = {node_id: (2 if node_id == 13 else 1) for node_id, _ in CLIENTS.values()}
check("shadow: each uninstrumented write counted against its own node", not_instrumented(), EXPECTED)
for name, (node_id, build) in CLIENTS.items():
    client, sink, read, write = build()
    with node_gate.node_gate(node_id):
        attempt(write)
        attempt(write)          # nested/repeated writes inside one passage
check("shadow: writes inside node_gate for the same node are not counted", not_instrumented(), EXPECTED)
client, sink, read, write = mikrotik()
with node_gate.node_gate(12):
    attempt(write)
check("shadow: a context of ANOTHER node does not instrument the write", not_instrumented().get(11), EXPECTED[11] + 1)

# ------------------------------------------------------------------ enforced
set_mode("enforced")
for name, (node_id, build) in CLIENTS.items():
    client, sink, read, write = build()
    before = len(sink.sent)
    write_result = attempt(write)
    after_write = len(sink.sent)
    read_result = normalized(attempt(read))
    check(f"enforced: {name} write refused before any byte; read still allowed",
          (write_result, after_write - before, read_result), ("refused", 0, "sent"))
unbound = MikrotikClient("10.9.9.9", "u", "p", 8728, False)
raw = FakeRouteros()
unbound._api = transport_guard.RouterosApi(raw, unbound)
check("enforced: a client built without a saved node cannot write",
      (attempt(lambda: unbound._api.path("ip", "address").add(address="1/1")), raw.sent), ("refused", []))
try:
    with node_gate.node_gate(11):
        entered = "entered"
except node_gate.GateNotAvailable:
    entered = "refused"
check("enforced: node_gate itself refuses in the parent process", entered, "refused")

# ------------------------------------------------------------------ stale cache
set_mode("off")
node_gate.reset_cache()
with node_gate.node_gate(11):
    pass                                          # caches 'off' with the current epoch
check("cache holds 'off'", node_gate._cached_runtime.gate_mode, "off")
with SessionLocal() as db:                        # another process switches to enforced
    row = db.get(mp.ProvisioningRuntimeState, 1)
    row.gate_mode, row.gate_mode_epoch = "enforced", row.gate_mode_epoch + 1
    row.owner_state, row.owner_host_id, row.owner_boot_id = "active", "e" * 64, str(uuid.uuid4())
    row.owner_claimed_at = row.owner_heartbeat_at = row.changed_at
    row.lock_backend, row.lock_verified_at = "flock", row.changed_at
    db.commit()
    new_epoch = row.gate_mode_epoch
gate_locks.write_mode_state(INSTALLATION, str(new_epoch))
client, sink, read, write = mikrotik()
check("stale cache: an uninstrumented write reads the mode fresh and is refused",
      (attempt(write), sink.sent), ("refused", []))
try:
    with node_gate.node_gate(11):
        stale = "entered with a stale mode"
except node_gate.GateNotAvailable:
    stale = "refused"
check("stale cache: node_gate sees the state file's new epoch and refreshes", stale, "refused")

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
