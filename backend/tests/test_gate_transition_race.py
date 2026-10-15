"""Gate-mode changes against writers in OTHER processes (Lifecycle 10.6,
10.7), on SQLite and real MariaDB (CI).

  * a process started AFTER the installation left its L0 state (owner,
    epochs) still finds the schema ready - the restart case;
  * a lock miss in another process is persisted at once: off -> shadow is
    refused by it (no 30 s flush window);
  * a write with no node_gate passage, started in another process while a
    change holds the gate-mode lock exclusively, sends NOTHING until the
    change committed, then decides on the new mode;
  * that uninstrumented 'shadow' write is visible to shadow -> enforced
    immediately, from the other process: the change is refused;
  * once 'enforced' is committed, a write from another process with no
    runner and no passage is refused before any byte;
  * the totals are read in a new transaction: a count committed after the
    caller's snapshot began is seen; a failed local flush stops the change.
"""
import contextlib
import datetime as dt
import os
import subprocess
import sys
import tempfile
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp
from app.services import gate_locks, node_gate, provisioning_control as control, provisioning_schema
from app.services import receipt_void_schema
from app.services.provisioning_host import HostIdentity

failures = []
CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_gate_race_child.py")


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine, lock_dir):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Factory)["ready"],
                                provisioning_schema.bootstrap(engine, Factory)["ready"]), (True, True))
    host = HostIdentity(("a1" if label == "sqlite" else "b2") * 32, str(uuid.uuid4()))
    with Factory() as db:
        node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.18.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.81.0.0/24")
        db.add(node)
        row = db.get(mp.ProvisioningRuntimeState, 1)
        now = dt.datetime.utcnow()
        row.owner_state, row.owner_host_id, row.owner_boot_id = "active", host.host_id, host.boot_id
        row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = now
        row.ownership_epoch, row.lock_backend = 1, "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.commit()
        node_id, installation = node.id, row.installation_uuid
    os.makedirs(gate_locks.installation_dir(installation, lock_dir), mode=0o700, exist_ok=True)
    path = os.path.join(lock_dir, "installation.id")
    with open(path, "w") as fh:
        fh.write(installation + "\n")
    os.chmod(path, 0o600)
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False), UM_LOCK_DIR=lock_dir)
    env.pop("MARIADB_TEST_URL", None)

    def child(action):
        return subprocess.Popen([sys.executable, CHILD, action, str(node_id)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def answer(process, timeout=120):
        out, _ = process.communicate(timeout=timeout)
        return out.strip().splitlines()[-1] if out.strip() else out

    def gate(target):
        with patch.object(control, "_identity", return_value=host), patch.object(control, "selftest"):
            try:
                control.change_gate_mode(Factory, 1, target, base_dir=lock_dir, timeout=20)
                return "changed"
            except HTTPException as exc:
                return exc.detail if isinstance(exc.detail, str) else exc.detail.get("code")

    def mode():
        with Factory() as db:
            return db.get(mp.ProvisioningRuntimeState, 1, populate_existing=True).gate_mode

    def reset():
        control.reset_gate_stats(Factory, 1)

    # ------------------------------------------------------------------ a miss in another process
    check(L + "another process misses the gate-mode lock in 'off'", answer(child("miss")), "missed")
    check(L + "...persisted at once: off -> shadow is refused by it", (gate("shadow"), mode()),
          ("mode_lock_miss_count_nonzero", "off"))
    reset()

    # ------------------------------------------------------------------ a write during the change
    held = gate_locks.FileLock(gate_locks.mode_lock_path(installation, lock_dir)).acquire(shared=False, timeout=0)
    writer = child("write")
    time.sleep(3.0)
    still_waiting = writer.poll() is None
    with Factory() as db:                         # the change commits under its exclusive hold
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.gate_mode, row.gate_mode_epoch = "shadow", row.gate_mode_epoch + 1
        db.commit()
        epoch = row.gate_mode_epoch
    gate_locks.write_mode_state(installation, str(epoch), lock_dir)
    held.release()
    result = answer(writer)
    check(L + "a write from another process during the change sends nothing until the change committed",
          still_waiting, True)
    check(L + "...then decides on the NEW mode: 'shadow' lets it through (uninstrumented)", result, "sent")
    with Factory() as db:
        missing = sum(r.writer_not_instrumented_count for r in db.query(mp.ProvisioningGateStat))
    check(L + "...and its uninstrumented count is committed before the send returned", missing >= 1, True)
    check(L + "shadow -> enforced refused by that other process's count, at once",
          (gate("enforced"), mode()), ("writer_not_instrumented_count_nonzero", "shadow"))
    reset()
    check(L + "after the reset: shadow -> enforced", (gate("enforced"), mode()), ("changed", "enforced"))
    check(L + "a write from another process with no runner and no passage: refused, nothing sent",
          answer(child("write")), "refused")

    # ------------------------------------------------------------------ the totals' transaction
    with Factory() as db:
        db.execute(select(mp.ProvisioningRuntimeState.gate_mode)).all()     # this transaction's snapshot
        with Factory() as other:
            other.add(mp.ProvisioningGateStat(node_id=node_id + 1000, mode_lock_miss_count=1))
            other.commit()
        totals = control._stat_totals(db)
    check(L + "totals read in a new transaction see a count committed after the caller's snapshot", totals[1], 1)
    reset()
    with patch.object(node_gate, "flush_counters", return_value=False):
        with Factory() as db:
            try:
                control._stat_totals(db)
                flushed = "read"
            except HTTPException as exc:
                flushed = exc.detail
    check(L + "a local flush that fails stops the change", flushed, "gate_counters_not_persisted")


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="gate_race_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    def run(label, engine):
        lock_dir = os.path.join(folder, f"locks-{label}")
        os.mkdir(lock_dir, 0o700)
        with patch.dict(os.environ, {"UM_LOCK_DIR": lock_dir}), \
                patch("app.database.SessionLocal", sessionmaker(bind=engine, autoflush=False)):
            node_gate.reset_cache()
            scenario(label, engine, lock_dir)
            node_gate.reset_cache()

    run("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        run("mariadb", maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
