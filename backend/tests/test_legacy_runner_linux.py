"""End to end on Linux: a legacy writer in 'enforced' really runs in the
runner child (Lifecycle 10.7, 10.8).

The parent calls legacy_remote.run(); a real runner process is spawned
(parent-death signal armed), takes the gate-mode and node locks itself,
builds LegacyChildGuard from the database and the host identity file, and
drives a guarded RouterOS client against a fake RouterOS API server on the
loopback interface (no node, no outside network). Checked:

  * the block's writes reach the router only from the child, and the
    result comes back to the caller;
  * the node lock is held by the child while it writes;
  * with the ownership taken over by another host between dispatch and the
    child's check, the child sends no write and the caller gets an error.
Linux only (prctl parent-death signal); SKIP elsewhere.
"""
import datetime as dt
import os
import socket
import sys
import tempfile
import threading
import uuid

if not sys.platform.startswith("linux"):
    print("SKIP  the runner child's write authority needs Linux (prctl parent-death signal)")
    sys.exit(0)

DIRECTORY = tempfile.mkdtemp(prefix="um-legacy-linux-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
os.environ["UM_HOST_ID_FILE"] = os.path.join(DIRECTORY, "host.id")
os.makedirs(os.environ["UM_LOCK_DIR"], mode=0o700)
with open(os.environ["UM_HOST_ID_FILE"], "wb") as fh:
    fh.write(os.urandom(32))
os.chmod(os.environ["UM_HOST_ID_FILE"], 0o600)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import gate_locks, legacy_remote, node_gate, provisioning_schema
from app.services.mikrotik_client import MikrotikClient, MikrotikError
from app.services.provisioning_host import read_identity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# ------------------------------------------------------------------ a fake RouterOS API server (loopback)
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
        self.server.listen(4)
        self.port = self.server.getsockname()[1]
        self.writes, self.lock_held_during_write = [], []

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
            payload = b"".join(_encode_length(len(w.encode())) + w.encode() for w in words) + b"\x00"
            conn.sendall(payload)

    def run(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            while True:
                words = self._sentence(conn)
                command = words[0] if words else ""
                if command.endswith("/print"):
                    self._send(conn, ["!re", "=.id=*7", "=comment=peer-a", "=interface=wg0", "=disabled=false"],
                               ["!done"])
                else:
                    if command != "/login":
                        self.writes.append(command)
                        probe = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, 51))
                        try:
                            probe.acquire(shared=False, timeout=0)
                            self.lock_held_during_write.append(False)
                            probe.release()
                        except gate_locks.LockTimeout:
                            self.lock_held_during_write.append(True)
                    self._send(conn, ["!done"])
        except (ConnectionError, OSError, IndexError):
            conn.close()


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
identity = read_identity()
router = FakeRouterOS()
router.start()
with SessionLocal() as db:
    db.add(models.Node(id=51, name="mt", type=models.NodeType.mikrotik, mt_host="127.0.0.1", mt_port=router.port,
                       mt_use_ssl=False, mt_username="admin", mt_password="pw", mt_wireguard_interface="wg0"))
    row = db.get(mp.ProvisioningRuntimeState, 1)
    INSTALLATION = row.installation_uuid
    now = dt.datetime.utcnow()
    row.gate_mode, row.gate_mode_epoch, row.lock_backend, row.lock_verified_at = "enforced", 1, "flock", now
    row.owner_state, row.owner_host_id, row.owner_boot_id = "active", identity.host_id, identity.boot_id
    row.owner_claimed_at = row.owner_heartbeat_at = now
    row.ownership_epoch = 1
    db.commit()
with open(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), "w") as fh:
    fh.write(INSTALLATION + "\n")
os.chmod(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), 0o600)
gate_locks.write_mode_state(INSTALLATION, "1")
node_gate.reset_cache()

with SessionLocal() as db:
    node = db.get(models.Node, 51)
    db.expunge(node)

result = legacy_remote.run(node, "wg_set_enabled", factory=MikrotikClient.for_node, interface="wg0",
                           peer_name="peer-a", enabled=False)
check("enforced: the write reached the router from the runner child and the result came back",
      (result, router.writes), (True, ["/interface/wireguard/peers/set"]))
check("the child held the node lock while writing", router.lock_held_during_write, [True])

# Another host takes over between dispatch and the child's own check.
real_owner = legacy_remote._owner_snapshot


def takeover(passage):
    snapshot = real_owner(passage)
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.owner_host_id, row.ownership_epoch = "9" * 64, row.ownership_epoch + 1
        db.commit()
    return snapshot


legacy_remote._owner_snapshot = takeover
router.writes.clear()
try:
    legacy_remote.run(node, "wg_set_enabled", factory=MikrotikClient.for_node, interface="wg0",
                      peer_name="peer-a", enabled=True)
    outcome = "returned"
except MikrotikError:
    outcome = "error"
finally:
    legacy_remote._owner_snapshot = real_owner
check("a takeover after dispatch: the child sends nothing and the caller gets an error",
      (outcome, router.writes), ("error", []))

router.server.close()
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
