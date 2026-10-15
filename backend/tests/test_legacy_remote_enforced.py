"""Legacy writers in 'enforced' run in the runner, fenced before every write
(Lifecycle 10.6-10.8).

Parent side (services/legacy_remote.py):
  * off/shadow: the block runs inline with the site's own client factory;
  * enforced: nothing is sent inline; a DTO goes to the runner with the node
    secrets and customer credentials ONLY in the two secret fields, the
    binding of THIS host's ownership and the node's endpoint fingerprint;
  * enforced but this host is not the owner (or has no identity): refused
    before the runner is even started;
  * a writer still using plain writer_gate in 'enforced' is refused;
  * client errors come back as the site's own exception class with a
    sanitized message; an unknown outcome is an error, never success.

Child side (services/provisioning_child_legacy.py), driven in-process with
the runner role simulated and a real guarded RouterOS client over a fake
socket API:
  * with the locks, ownership and endpoint intact the block's writes go out;
  * a changed ownership epoch, a changed node endpoint, a gate that left
    'enforced', missing locks: no byte is sent;
  * the fence is re-checked before EVERY write: a change between the two
    writes of one block stops the second one.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-legacy-enforced-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
os.makedirs(os.environ["UM_LOCK_DIR"], mode=0o700)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import gate_locks, legacy_remote, node_gate, provisioning_child_authority as authority
from app.services import provisioning_child_entry as entry, provisioning_child_legacy as child
from app.services import provisioning_host, provisioning_schema, remote_runner
from app.services.mikrotik_client import MikrotikError
from app.services.provisioning_host import HostIdentity
from app.services.remote_action import Outcome, RemoteActionResult
from app.services.transport_guard import GateViolation

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
IDENTITY = HostIdentity("f" * 64, str(uuid.uuid4()))
with SessionLocal() as db:
    db.add(models.Node(id=31, name="mt", type=models.NodeType.mikrotik, mt_host="10.4.4.4", mt_username="admin",
                       mt_password="NODE-PASSWORD-SENTINEL", mt_wireguard_interface="wg0",
                       mt_radius_secret="RADIUS-SECRET-SENTINEL"))
    row = db.get(mp.ProvisioningRuntimeState, 1)
    INSTALLATION = row.installation_uuid
    db.commit()
with open(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), "w") as fh:
    fh.write(INSTALLATION + "\n")
os.chmod(os.path.join(os.environ["UM_LOCK_DIR"], "installation.id"), 0o600)


def node():
    with SessionLocal() as db:
        value = db.get(models.Node, 31)
        db.expunge(value)
        return value


def set_runtime(mode, *, owner=IDENTITY, epoch=1):
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        now = dt.datetime.utcnow()
        row.gate_mode_epoch += 1
        row.gate_mode, row.ownership_epoch = mode, epoch
        row.lock_backend, row.lock_verified_at = ("flock", now) if mode != "off" else ("none", None)
        if mode == "enforced" or owner is not None:
            row.owner_state, row.owner_host_id, row.owner_boot_id = "active", owner.host_id, owner.boot_id
            row.owner_claimed_at = row.owner_heartbeat_at = now
        db.commit()
        gate_locks.write_mode_state(INSTALLATION, str(row.gate_mode_epoch))
    node_gate.reset_cache()


class FakeRouteros:
    """Stands for the socket-level librouteros API: records every command."""

    def __init__(self, on_write=None):
        self.sent, self.on_write = [], on_write

    def __call__(self, command, **kwargs):
        if not command.endswith("/print"):
            self.sent.append(command)
            if self.on_write:
                self.on_write(len(self.sent))
            yield {"ret": "*1"}
            return
        yield {".id": "*7", "comment": "peer-a", "name": "wg0", "interface": "wg0"}

    def close(self):
        pass


class Recorder:
    """A legacy client stand-in for the inline path (the site's factory seam)."""

    def __init__(self):
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def list_peers(self, interface):
        self.calls.append("list_peers")
        return [{".id": "*7", "comment": "peer-a"}]

    def set_peer_disabled(self, peer_id, disabled):
        self.calls.append(f"set_peer_disabled:{disabled}")


# ------------------------------------------------------------------ parent: off / shadow inline
for mode in ("off", "shadow"):
    set_runtime(mode, owner=IDENTITY if mode == "shadow" else None)
    recorder = Recorder()
    with patch.object(remote_runner, "run_action", side_effect=AssertionError("no runner in " + mode)):
        result = legacy_remote.run(node(), "wg_set_enabled", factory=lambda n: recorder, interface="wg0",
                                   peer_name="peer-a", enabled=False)
    check(f"{mode}: the block runs inline with the site's factory", (result, recorder.calls),
          (True, ["list_peers", "set_peer_disabled:True"]))

# ------------------------------------------------------------------ parent: enforced dispatch
set_runtime("enforced")
captured = []


def fake_runner(dto):
    captured.append(dto)
    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.SUCCEEDED, write_attempted=True,
                              observed={"value": ["cert-1"]})


with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
        patch.object(remote_runner, "run_action", side_effect=fake_runner):
    recorder = Recorder()
    value = legacy_remote.run(node(), "mt_push", factory=lambda n: recorder,
                              action_type=legacy_remote.PUSH_ACTIONS["push_radius_config"],
                              calls=[["push_radius_config", {"secret": "RADIUS-SECRET-SENTINEL", "panel_host": "h"}]])
dto = captured[-1]
check("enforced: nothing ran inline; the runner answered", (recorder.calls, value), ([], ["cert-1"]))
check("enforced: the DTO is bound to this host's ownership, the gate epoch and the node endpoint",
      (dto.fencing["binding"]["phase"], dto.fencing["binding"]["ownership_epoch"], dto.fencing["binding"]["node_id"],
       dto.fencing["binding"]["backend"], len(dto.fencing["binding"]["endpoint_fingerprint"]),
       dto.fencing["expected_parent_pid"]),
      ("legacy", 1, 31, "mikrotik_wg", 64, os.getpid()))
public = repr(dto) + str(dto.params) + str(dto.node_config) + str(dto.fencing)
check("enforced: node password and RADIUS secret travel only in the secret fields",
      ("NODE-PASSWORD-SENTINEL" in public, "RADIUS-SECRET-SENTINEL" in public,
       dto.node_secrets.get("mt_password"), "calls" in dto.credential),
      (False, False, "NODE-PASSWORD-SENTINEL", True))

with patch.object(provisioning_host, "read_identity", return_value=HostIdentity("1" * 64, str(uuid.uuid4()))), \
        patch.object(remote_runner, "run_action", side_effect=AssertionError("must not spawn")):
    try:
        legacy_remote.run(node(), "wg_set_enabled", interface="wg0", peer_name="peer-a", enabled=False)
        refused = "ran"
    except GateViolation:
        refused = "refused"
check("enforced on a host that is not the owner: refused before any runner", refused, "refused")
with patch.object(provisioning_host, "read_identity", side_effect=provisioning_host.HostIdentityUnavailable("x")), \
        patch.object(remote_runner, "run_action", side_effect=AssertionError("must not spawn")):
    try:
        legacy_remote.run(node(), "wg_set_enabled", interface="wg0", peer_name="peer-a", enabled=False)
        refused = "ran"
    except GateViolation:
        refused = "refused"
check("enforced without a host identity: refused before any runner", refused, "refused")
try:
    with node_gate.writer_gate(node()):
        unconverted = "entered"
except node_gate.GateNotAvailable:
    unconverted = "refused"
check("enforced: a writer still on plain writer_gate is refused", unconverted, "refused")

for result, expected in (
        (RemoteActionResult(action_id=str(uuid.uuid4()), outcome=Outcome.TRANSPORT_ERROR, write_attempted=True,
                            error_code="client_error", error_sanitized="peer busy"), ("MikrotikError", "peer busy")),
        (RemoteActionResult.killed_unknown(str(uuid.uuid4()), "hard_deadline_exceeded"), ("MikrotikError", "unknown"))):
    with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
            patch.object(remote_runner, "run_action", side_effect=lambda d, r=result: RemoteActionResult(
                **{**r.__dict__, "action_id": d.action_id})):
        try:
            legacy_remote.run(node(), "wg_set_enabled", interface="wg0", peer_name="peer-a", enabled=False)
            got = ("returned", "")
        except MikrotikError as exc:
            got = (type(exc).__name__, "unknown" if "نامعلوم" in str(exc) else str(exc))
    check(f"enforced: runner {result.outcome.value}/{result.error_code} -> the site's exception, never success",
          got, expected)
check("sanitize removes URLs and secret values",
      child.sanitize("failed http://10.0.0.1/secret-path/abc?token=1 with pw NODE-PASSWORD-SENTINEL",
                     ["NODE-PASSWORD-SENTINEL"]), "failed <url> with pw ***")

# ------------------------------------------------------------------ child, in-process (runner role simulated)
def child_run(dto, raw, *, hold_locks=True, before=None):
    """What remote_runner_child.main does around an action, in this process."""
    mode_lock = gate_locks.FileLock(gate_locks.mode_lock_path(INSTALLATION)).acquire(shared=True, timeout=0)
    node_lock = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, 31)).acquire(shared=False, timeout=0)
    try:
        if before:
            before()
        with patch.object(authority, "_protected_parent", return_value=True), \
                authority._runner_context(os.getppid()), \
                patch("librouteros.connect", return_value=raw), \
                patch.object(provisioning_host, "read_identity", return_value=IDENTITY):
            context = entry.locked_context(mode_lock, node_lock) if hold_locks else contextlib.nullcontext()
            with context:
                try:
                    return child.run(dto)
                except Exception as exc:  # noqa: BLE001 - the real child reports this as action_raised
                    return RemoteActionResult(action_id=dto.action_id, outcome=Outcome.TRANSPORT_ERROR,
                                              write_attempted=True, error_code="action_raised",
                                              error_sanitized=type(exc).__name__)
    finally:
        node_lock.release()
        mode_lock.release()


def dispatch_dto(block, **arguments):
    set_runtime("enforced")
    with patch.object(provisioning_host, "read_identity", return_value=IDENTITY), \
            patch.object(remote_runner, "run_action", side_effect=lambda d: captured.append(d) or RemoteActionResult(
                action_id=d.action_id, outcome=Outcome.SUCCEEDED, observed={"value": None})):
        legacy_remote.run(node(), block, **arguments)
    return captured[-1]


dto = dispatch_dto("wg_bounce", interface="wg0", peer_name="peer-a")
raw = FakeRouteros()
result = child_run(dto, raw)
check("child: locks, ownership and endpoint intact -> both writes of the block go out",
      (result.outcome, result.observed.get("value"), raw.sent), (Outcome.SUCCEEDED, True, ["/interface/wireguard/peers/set"] * 2))


def mutate(**values):
    def apply():
        with SessionLocal() as db:
            if "mt_host" in values:
                db.get(models.Node, 31).mt_host = values["mt_host"]
            row = db.get(mp.ProvisioningRuntimeState, 1)
            for key in ("ownership_epoch", "gate_mode"):
                if key in values:
                    setattr(row, key, values[key])
            db.commit()
    return apply


for label, change in (("ownership epoch changed", mutate(ownership_epoch=2)),
                      ("node endpoint changed", mutate(mt_host="10.9.9.9")),
                      ("gate no longer enforced", mutate(gate_mode="shadow"))):
    dto = dispatch_dto("wg_bounce", interface="wg0", peer_name="peer-a")
    raw = FakeRouteros()
    result = child_run(dto, raw, before=change)
    check(f"child: {label} after dispatch -> refused, nothing sent", (result.outcome, raw.sent),
          (Outcome.TRANSPORT_ERROR, []))
    mutate(ownership_epoch=1, mt_host="10.4.4.4", gate_mode="enforced")()

dto = dispatch_dto("wg_bounce", interface="wg0", peer_name="peer-a")
raw = FakeRouteros()
result = child_run(dto, raw, hold_locks=False)
check("child: without the runner's locks -> refused, nothing sent", (result.outcome, raw.sent), (Outcome.TRANSPORT_ERROR, []))

dto = dispatch_dto("wg_bounce", interface="wg0", peer_name="peer-a")
raw = FakeRouteros(on_write=lambda n: n == 1 and mutate(ownership_epoch=5)())
result = child_run(dto, raw)
check("child: the fence is re-checked before EVERY write (a takeover between two writes stops the second)",
      (result.outcome, raw.sent), (Outcome.TRANSPORT_ERROR, ["/interface/wireguard/peers/set"]))
mutate(ownership_epoch=1)()

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
