"""Regression tests for four review findings of the P6 batch.

1. transport_guard in a cold process (no mode ever read) with an UNREADABLE
   runtime row refuses a remote write; a process that already saw 'off'
   keeps acting on it; a database without the provisioning table is 'no gate'.
2. an 'off' node_gate context that did NOT hold the shared mode lock does not
   authorize a write: the mode is read fresh (a change may be in progress).
3. durable add_connection with a purchase_batch attaches to the one locked
   Purchase whose connections carry that batch; an unknown batch with several
   services, or a batch shared by two services, stays 409.
4. flush_counters never reports success after its retries ran out, and never
   loses or double-counts a delta (partial failure across nodes).
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-p6-review-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError, OperationalError

from app import database, models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router
from app.services import bot_auth, gate_locks, node_gate, provisioning_contracts as contracts
from app.services import provisioning_durable as durable, provisioning_parent_execute as execute
from app.services import provisioning_schema, receipt_void_schema, remote_action as action, transport_guard
from app.services import user_ops, wallet_accounts
from app.services.mikrotik_client import MikrotikClient
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class FakeRouteros:
    def __init__(self):
        self.sent = []

    def __call__(self, command, **kwargs):
        if not command.endswith("/print"):
            self.sent.append(command)
        yield {"ret": "*1"}

    def close(self):
        pass


def write_with(client):
    raw = FakeRouteros()
    client._api = transport_guard.RouterosApi(raw, client)
    try:
        client._api.path("ip", "address").add(address="1.1.1.1/32")
        return "sent", raw.sent
    except transport_guard.GateViolation:
        return "refused", raw.sent


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
with SessionLocal() as db:
    db.add(models.PanelSettings(id=1))
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.8.8.8",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.11.0.0/24")
    db.add(node)
    db.commit()
    NODE = node.id
    INSTALLATION = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid


def node_obj():
    with SessionLocal() as db:
        value = db.get(models.Node, NODE)
        db.expunge(value)
        return value


# ------------------------------------------------------------------ 1. cold + unreadable
class Broken:
    def __call__(self):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


node_gate.reset_cache()
with patch.object(database, "SessionLocal", Broken()):
    check("1. cold process, unreadable runtime: the write is refused and nothing sent",
          write_with(MikrotikClient.for_node(node_obj())), ("refused", []))
node_gate._cached_runtime = node_gate.GateRuntime(INSTALLATION, "off", 0)
with patch.object(database, "SessionLocal", Broken()):
    check("1. a process that already saw 'off', NO mode-state file: unreadable now is refused (nothing proven)",
          write_with(MikrotikClient.for_node(node_obj())), ("refused", []))
gate_locks.write_mode_state(INSTALLATION, "0")
with patch.object(database, "SessionLocal", Broken()):
    check("1. ...with the state file naming the cached epoch: proven, it keeps acting on 'off'",
          write_with(MikrotikClient.for_node(node_obj()))[0], "sent")
os.remove(gate_locks.mode_state_path(INSTALLATION))
node_gate.reset_cache()


class NoTable:
    def __call__(self):
        raise OperationalError("SELECT", {}, Exception("no such table: provisioning_runtime_state"))


with patch.object(database, "SessionLocal", NoTable()):
    check("1. a database without the provisioning table is 'no gate' (writes as today)",
          write_with(MikrotikClient.for_node(node_obj()))[0], "sent")

# ------------------------------------------------------------------ 2. off context without the mode lock
with SessionLocal() as db:
    row = db.get(mp.ProvisioningRuntimeState, 1)
    now = dt.datetime.utcnow()
    row.gate_mode, row.gate_mode_epoch, row.lock_backend, row.lock_verified_at = "enforced", 1, "flock", now
    row.owner_state, row.owner_host_id, row.owner_boot_id = "active", "a" * 64, str(uuid.uuid4())
    row.owner_claimed_at = row.owner_heartbeat_at = now
    row.ownership_epoch = 1
    db.commit()
for held, expected in ((False, "refused"), (True, "sent")):
    context = node_gate.ShadowGateContext(node_id=NODE, mode="off", gate_mode_epoch=0, mode_lock_held=held,
                                          node_lock_held=False)
    mark = node_gate._current.set(context)
    try:
        got = write_with(MikrotikClient.for_node(node_obj()))[0]
    finally:
        node_gate._current.reset(mark)
    check(f"2. an 'off' context {'holding' if held else 'WITHOUT'} the shared mode lock while the DB says enforced: "
          f"{expected}", got, expected)

# ------------------------------------------------------------------ 3. add_connection and purchase_batch
HOST = HostIdentity("a" * 64, str(uuid.uuid4()))
with SessionLocal() as db:
    row = db.get(mp.ProvisioningRuntimeState, 1)
    row.owner_host_id, row.owner_boot_id = HOST.host_id, HOST.boot_id
    db.get(mp.ProvisioningTypeMode, "add_connection").mode = "durable"
    admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    db.add(admin)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=NODE, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(db.get(models.Node, NODE)),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=admin.id,
        verified_at=dt.datetime.utcnow()))
    user = wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=99)
    db.flush()
    services = [models.Purchase(user_id=user.id, quota_bytes=1, status=models.UserStatus.active) for _ in range(3)]
    db.add_all(services)
    db.flush()
    A, B, C = (s.id for s in services)
    for purchase_id, batch in ((A, "batch-a"), (B, "shared"), (C, "shared")):
        db.add(models.Connection(user_id=user.id, node_id=NODE, type=models.ConnectionType.openvpn, purchase_id=purchase_id,
                                 purchase_batch=batch, ppp_username=f"u{purchase_id}", ppp_password="p"))
    db.commit()
durable.read_identity = lambda: HOST
gate_locks.write_mode_state(INSTALLATION, "1")
node_gate.reset_cache()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)


def add(batch):
    with SessionLocal() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")), \
            patch.object(execute.remote_runner, "run_action", side_effect=lambda d: action.RemoteActionResult(
                d.action_id, action.Outcome.SUCCEEDED, write_attempted=True)):
        spec = schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard", purchase_batch=batch)
        try:
            info = bot_router.add_connection("cust", spec, db=db, owner_admin_id=None, principal=PRINCIPAL,
                                             x_idempotency_key=str(uuid.uuid4()))
            return db.get(models.Connection, info.id).purchase_id
        except HTTPException as exc:
            return exc.detail


check("3. a purchase_batch carried by one service: the new connection joins exactly that service", add("batch-a"), A)
check("3. a batch shared by two services: ambiguous, refused", add("shared"), "purchase_target_required")
check("3. an unknown batch with several services: still refused", add("nobody"), "purchase_target_required")

# ------------------------------------------------------------------ 4. flush_counters
node_gate.counters.reset()
node_gate.counters.add("contention", 5)


class AlwaysConflict:
    def __call__(self):
        class Session:
            def execute(self, *a, **k):
                raise IntegrityError("INSERT", {}, Exception("duplicate"))

            def rollback(self):
                pass

            def commit(self):
                pass

            def close(self):
                pass
        return Session()


check("4. retries exhausted: reported as failure, not success", node_gate.flush_counters(AlwaysConflict()), False)
check("4. ...and the delta is kept for the next flush", node_gate.counters._pending, {5: {"contention": 1}})
node_gate.counters.add("contention", 6)
calls = {"n": 0}
real = SessionLocal


def flaky():
    session = real()
    calls["n"] += 1
    if calls["n"] >= 2:                           # the second node's write fails hard
        session.execute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database down"))
    return session


check("4. a hard failure on the second node: reported as failure", node_gate.flush_counters(flaky), False)
check("4. ...only the unwritten node's delta is kept (the written one is not counted twice)",
      node_gate.counters._pending, {6: {"contention": 1}})
check("4. a later flush writes it", node_gate.flush_counters(SessionLocal), True)
with SessionLocal() as db:
    check("4. each node counted exactly once in the end",
          {row.node_id: row.contention_count for row in db.query(mp.ProvisioningGateStat)}, {5: 1, 6: 1})

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
