"""Heartbeat and clean-shutdown release of host ownership (Lifecycle 10.4).

* heartbeat touches only owner_heartbeat_at, only when the owner is exactly
  this host and boot, and never bumps the CAS version;
* shutdown first refuses new runners and kills+reaps a live one, then
  releases ownership with epoch+1 and a 'shutdown_release' event (actor 0);
* another owner is never released; with the gate 'enforced' the row is kept
  unless the installed CHECK allows a released owner.
"""
import datetime as dt
import os
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-host-lifecycle-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from app import models, models_provisioning as mp
from app.database import SessionLocal, engine
from app.services import provisioning_host_lifecycle as lifecycle, provisioning_schema, remote_runner
from app.services.provisioning_host import HostIdentity
from app.services.remote_action import ActionType, Outcome, RemoteActionDTO

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("provisioning schema ready", provisioning_schema.bootstrap(engine, SessionLocal)["ready"])
ME = HostIdentity("a1" * 32, str(uuid.uuid4()))
OTHER = HostIdentity("b2" * 32, str(uuid.uuid4()))


def own(identity, gate="shadow"):
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        old = dt.datetime(2020, 1, 1)
        row.owner_state, row.owner_host_id, row.owner_boot_id = "active", identity.host_id, identity.boot_id
        row.owner_claimed_at = row.owner_heartbeat_at = old
        row.ownership_epoch, row.lock_backend, row.lock_verified_at, row.gate_mode = 3, "flock", old, gate
        db.commit()


def state():
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        return row.owner_state, row.ownership_epoch, row.version, row.owner_heartbeat_at


with patch.object(lifecycle, "read_identity", return_value=ME):
    own(OTHER)
    before = state()
    check("heartbeat of a host that is not the owner changes nothing", (lifecycle.heartbeat(), state()), (False, before))
    own(ME)
    before = state()
    beat = lifecycle.heartbeat()
    after = state()
    check("heartbeat of the owner: only the heartbeat moves, version kept",
          (beat, after[:3], after[3] > before[3]), (True, before[:3], True))

    own(OTHER)
    check("shutdown never releases another host's ownership", lifecycle.shutdown_release(grace_seconds=0), "not_owner")
    remote_runner._closing = False
    own(ME, gate="enforced")
    with patch.object(lifecycle.provisioning_runtime_contract, "supports_release", return_value=False):
        check("enforced gate without a release-capable CHECK: ownership kept", (
            lifecycle.shutdown_release(grace_seconds=0), state()[0]), ("kept", "active"))
    remote_runner._closing = False

    # Restart safety: what must run before this host may write through 'enforced'.
    def action(identity):
        with SessionLocal() as db:
            return lifecycle.ownership_action(db.get(mp.ProvisioningRuntimeState, 1), identity)

    rebooted = HostIdentity(ME.host_id, str(uuid.uuid4()))
    check("restart on the same host boot (row kept): nothing to run, the same owner writes on",
          action(ME), None)
    check("the same host after a reboot (new boot id): an authenticated reclaim first", action(rebooted), "reclaim")
    with patch.object(lifecycle.logger, "error") as said:
        with patch.object(lifecycle, "_me", return_value=OTHER):
            foreign = lifecycle.startup_check(SessionLocal)
    check("another host owns it: 'foreign_owner', no route suggested, a manual-review error at startup",
          (action(OTHER), foreign, lifecycle.OWNERSHIP_ACTIONS["foreign_owner"], "Manual review" in str(said.call_args)),
          ("foreign_owner", "foreign_owner", None, True))
    check("no host identity: no write", action(None), "host_identity_unavailable")
    with patch.object(lifecycle, "_me", return_value=rebooted), \
            patch.object(lifecycle.logger, "error") as said:
        check("startup with 'enforced' and a reboot: says so as an error, names the route",
              (lifecycle.startup_check(SessionLocal), said.call_count, "ownership/reclaim" in str(said.call_args)),
              ("reclaim", 1, True))

    # A live runner is killed and reaped before the release.
    own(ME, gate="shadow")
    results = {}
    worker = threading.Thread(target=lambda: results.setdefault("r", remote_runner.run_action(RemoteActionDTO(
        action_type=ActionType.SELFTEST_SLEEP, params={"seconds": 30}, timeouts={"hard_deadline": 60}))))
    worker.start()
    for _ in range(100):
        if remote_runner.live_count():
            break
        time.sleep(0.05)
    started = remote_runner.live_count()
    outcome = lifecycle.shutdown_release(grace_seconds=0.3)
    worker.join(30)
    check("the live runner was running, then killed and reaped; then ownership released",
          (started, results["r"].outcome, remote_runner.live_count(), outcome), (1, Outcome.KILLED_UNKNOWN, 0, "released"))
    with SessionLocal() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        event = db.query(mp.ProvisioningOwnershipEvent).filter_by(event_type="shutdown_release").one()
        check("released row: no owner, epoch + 1; event with actor 0",
              (row.owner_state, row.owner_host_id, row.ownership_epoch, event.actor_admin_id, event.ownership_epoch),
              ("released", None, 4, 0, 4))
    check("released at a shutdown outside 'enforced': an authenticated claim before 'enforced'", action(ME), "claim")
    try:
        remote_runner.run_action(RemoteActionDTO(action_type=ActionType.SELFTEST_ECHO))
        refused = "started"
    except remote_runner.RunnerShuttingDown:
        refused = "refused"
    check("after shutdown no new runner starts", refused, "refused")
    remote_runner._closing = False

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
