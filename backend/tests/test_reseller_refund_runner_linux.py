"""Unpaid-service cancellation with the provisioning gate ENFORCED (as on
production): its RouterOS writes run in the runner child, never in the
panel process. Linux only (the runner child's parent-death signal); SKIP
elsewhere.

A real runner child talks to a stateful fake RouterOS API server on the
loopback interface (no node, no outside network):

  * a direct write from the panel process is refused by the gate - the
    reason this path must go through the runner;
  * the cancellation's stop and delete reach the router only from the
    child, with the node lock held;
  * the delete fails: the operation stays pending with the final counters
    durable, no credit, the service in place;
  * resumed: the same peer (still stopped, same counters) is deleted and the
    reseller is credited exactly once; a retry returns the stored result;
  * the customer's other service, its peer and the wallet are untouched.
"""
import datetime as dt
import os
import socket
import sys
import tempfile
import threading
from unittest.mock import patch

if not sys.platform.startswith("linux"):
    print("SKIP  the runner child's write authority needs Linux (prctl parent-death signal)")
    sys.exit(0)

DIRECTORY = tempfile.mkdtemp(prefix="um-refund-runner-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
os.environ["UM_HOST_ID_FILE"] = os.path.join(DIRECTORY, "host.id")
os.environ["BOT_DB_PATH"] = os.path.join(DIRECTORY, "bot.db")
os.makedirs(os.environ["UM_LOCK_DIR"], mode=0o700)
with open(os.environ["UM_HOST_ID_FILE"], "wb") as fh:
    fh.write(os.urandom(32))
os.chmod(os.environ["UM_HOST_ID_FILE"], 0o600)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException

from app import models, models_provisioning as mp
from app.config import settings
from app.database import SessionLocal, engine
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from app.services import gate_locks, node_gate, provisioning_schema
from app.services import reseller_refund_worker as worker
from app.services.mikrotik_client import MikrotikClient, MikrotikError
from app.services.provisioning_host import read_identity
from app.services.reseller_refund import bind_sale
from app.services.transport_guard import GateViolation

failures = []
NODE_ID = 61


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# ------------------------------------------------------------------ a stateful fake RouterOS API (loopback)
def _encode_length(n):
    if n < 0x80:
        return bytes([n])
    if n < 0x4000:
        return (n | 0x8000).to_bytes(2, "big")
    return (n | 0xC00000).to_bytes(3, "big")


def _read_length(conn):
    first = conn.recv(1)
    if not first:
        raise ConnectionError()
    b = first[0]
    if b < 0x80:
        return b
    if b < 0xC0:
        return ((b & 0x3F) << 8) | conn.recv(1)[0]
    rest = conn.recv(2)
    return ((b & 0x1F) << 16) | (rest[0] << 8) | rest[1]


class FakeRouterOS(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(8)
        self.port = self.server.getsockname()[1]
        self.peers = {
            "*1": {"interface": "wg0", "comment": "peer-a", "public-key": "key-a", "allowed-address": "10.0.0.2/32",
                   "disabled": "false", "tx": "30", "rx": "0"},
            "*2": {"interface": "wg0", "comment": "peer-b", "public-key": "key-b", "allowed-address": "10.0.0.3/32",
                   "disabled": "false", "tx": "999", "rx": "5"},
        }
        self.fail_remove = 0
        self.writes, self.lock_held = [], []
        self.mutex = threading.Lock()

    def _sentence(self, conn):
        words = []
        while True:
            length = _read_length(conn)
            if length == 0:
                return words
            data = b""
            while len(data) < length:
                data += conn.recv(length - len(data))
            words.append(data.decode())

    def _send(self, conn, *sentences):
        for words in sentences:
            conn.sendall(b"".join(_encode_length(len(w.encode())) + w.encode() for w in words) + b"\x00")

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _note_write(self, command):
        self.writes.append(command)
        probe = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, NODE_ID))
        try:
            probe.acquire(shared=False, timeout=0)
            self.lock_held.append(False)
            probe.release()
        except gate_locks.LockTimeout:
            self.lock_held.append(True)

    def _serve(self, conn):
        try:
            while True:
                words = self._sentence(conn)
                command = words[0] if words else ""
                args = dict(w[1:].split("=", 1) for w in words[1:] if w.startswith("="))
                with self.mutex:
                    if command == "/login":
                        self._send(conn, ["!done"])
                    elif command == "/interface/wireguard/peers/print":
                        rows = [["!re", f"=.id={pid}"] + [f"={k}={v}" for k, v in p.items()]
                                for pid, p in self.peers.items()]
                        self._send(conn, *rows, ["!done"])
                    elif command == "/system/resource/print":
                        self._send(conn, ["!re", "=uptime=52w"], ["!done"])
                    elif command.endswith("/print"):
                        self._send(conn, ["!done"])
                    elif command == "/interface/wireguard/peers/set":
                        self._note_write(command)
                        self.peers[args[".id"]]["disabled"] = "true" if args.get("disabled") == "yes" else "false"
                        self._send(conn, ["!done"])
                    elif command == "/interface/wireguard/peers/remove":
                        self._note_write(command)
                        if self.fail_remove:
                            self.fail_remove -= 1
                            self._send(conn, ["!trap", "=message=failure: injected"], ["!done"])
                        else:
                            self.peers.pop(args[".id"], None)
                            self._send(conn, ["!done"])
                    else:
                        self._note_write(command)
                        self._send(conn, ["!done"])
        except (ConnectionError, OSError, IndexError, KeyError):
            conn.close()


# ------------------------------------------------------------------ an enforced gate owned by this host
models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
identity = read_identity()
router = FakeRouterOS()
router.start()
with SessionLocal() as db:
    row = db.get(mp.ProvisioningRuntimeState, 1)
    INSTALLATION = row.installation_uuid
    now = dt.datetime.utcnow()
    row.gate_mode, row.gate_mode_epoch, row.lock_backend, row.lock_verified_at = "enforced", 1, "flock", now
    row.owner_state, row.owner_host_id, row.owner_boot_id = "active", identity.host_id, identity.boot_id
    row.owner_claimed_at = row.owner_heartbeat_at = now
    row.ownership_epoch = 1
    actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", balance=0)
    node = models.Node(id=NODE_ID, name="mt", type=models.NodeType.mikrotik, mt_host="127.0.0.1",
                       mt_port=router.port, mt_use_ssl=False, mt_username="admin", mt_password="pw",
                       mt_wireguard_interface="wg0")
    package = models.Package(name="test", duration_days=10, price=100000, quota_gb=1)
    db.add_all([actor, node, package])
    db.flush()
    user = models.User(username="customer", owner_admin_id=actor.id, balance=456, purchase_count=2)
    db.add(user)
    db.flush()
    target, other = (models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=100, used_bytes=20,
                                     expire_at=dt.datetime.utcnow() + dt.timedelta(days=8)) for _ in range(2))
    db.add_all([target, other])
    db.flush()
    db.add_all([
        models.Connection(user_id=user.id, purchase_id=target.id, node_id=NODE_ID, type=models.ConnectionType.wireguard,
                          wg_peer_name="peer-a", wg_public_key="key-a", wg_client_address="10.0.0.2/32",
                          total_bytes=20, last_rx_bytes=20, last_tx_bytes=0),
        models.Connection(user_id=user.id, purchase_id=other.id, node_id=NODE_ID, type=models.ConnectionType.wireguard,
                          wg_peer_name="peer-b", wg_public_key="key-b", wg_client_address="10.0.0.3/32",
                          total_bytes=20, last_rx_bytes=20, last_tx_bytes=0)])
    debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
    sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id, purchase_id=target.id)
    db.add_all([debit, sale])
    db.flush()
    bind_sale(db, target, debit, package, sale_entry=sale, purchase_count_delta=1)
    db.commit()
    PID, OTHER, UID, AID = target.id, other.id, user.id, actor.id
with open(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), "w") as fh:
    fh.write(INSTALLATION + "\n")
os.chmod(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), 0o600)
gate_locks.write_mode_state(INSTALLATION, "1")
node_gate.reset_cache()

# ------------------------------------------------------------------ why the runner: a direct write is refused
with SessionLocal() as db:
    direct = db.get(models.Node, NODE_ID)
    try:
        with MikrotikClient.for_node(direct) as mt:
            mt.set_peer_disabled("*1", disabled=True)
        outcome = "sent"
    except (GateViolation, MikrotikError) as exc:
        outcome = "refused" if "outside the runner while the gate is enforced" in str(exc) else str(exc)
check("enforced: a write from the panel process itself is refused by the gate, nothing sent",
      (outcome, router.writes, router.peers["*1"]["disabled"]), ("refused", [], "false"))


def execute():
    with SessionLocal() as db:
        actor = db.get(models.AdminUser, AID)
        try:
            return worker.execute(db, purchase_id=PID, actor=actor)
        except HTTPException as exc:
            return exc.status_code, exc.detail


def state():
    with SessionLocal() as db:
        op = db.query(Operation).one_or_none()
        step = db.query(Step).one_or_none()
        return dict(op=op.state if op else None, captured=bool(step and step.captured_at),
                    verified=bool(step and step.verified_at), seller=db.get(models.AdminUser, AID).balance,
                    purchase=db.get(models.Purchase, PID) is not None,
                    refunds=db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count())


with patch.object(settings, "reseller_cancellation_enabled", True):
    with SessionLocal() as db:
        check("eligible on this enforced install", worker.availability(db, db.get(models.Purchase, PID)), None)

    # ---------------------------------------------------------------- the delete fails
    router.fail_remove = 1
    failed = execute()
    check("delete failed: the cancellation is pending with the reason, not settled",
          (failed[0], failed[1]["code"], failed[1]["reason"]),
          (409, "reseller_cancellation_pending", "wg_refund_delete_unconfirmed"))
    check("...the peer was stopped (disabled) and is still there",
          ("*1" in router.peers, router.peers.get("*1", {}).get("disabled")), (True, "true"))
    check("...its final counters are durable; no credit; the service is still in the panel",
          state(), dict(op="pending", captured=True, verified=False, seller=0, purchase=True, refunds=0))
    check("...every write came from the runner child holding the node lock",
          (bool(router.writes), all(router.lock_held)), (True, True))

    # ---------------------------------------------------------------- resumed
    router.writes.clear()
    router.lock_held.clear()
    done = execute()
    check("resumed: deleted and settled - the reseller credited once", done.get("refund_amount"), 75000)
    check("...the peer is gone from the router", "*1" in router.peers, False)
    check("...writes again only from the child with the node lock", (bool(router.writes), all(router.lock_held)),
          (True, True))
    check("...settled state", state(), dict(op="completed", captured=True, verified=True, seller=75000,
                                            purchase=False, refunds=1))
    again = execute()
    check("a retry returns the stored result and credits nothing more",
          (again == done, state()["seller"], state()["refunds"]), (True, 75000, 1))

    # ---------------------------------------------------------------- the rest is untouched
    with SessionLocal() as db:
        kept = db.get(models.Purchase, OTHER)
        check("the customer's other service, its connection and the wallet are untouched",
              (kept is not None, len(kept.connections), db.get(models.User, UID).balance), (True, 1, 456))
    check("...and its peer on the router is still enabled with its counters",
          (router.peers["*2"]["disabled"], router.peers["*2"]["tx"]), ("false", "999"))

router.server.close()
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
