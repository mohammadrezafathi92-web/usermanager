"""A failed read of the gate mode never lets a cached 'off' through
(Lifecycle 10.6, 10.7), on SQLite and real MariaDB (CI).

The scenario: this process has the mode 'off' cached; ANOTHER process
commits a change to 'enforced' (and releases the gate-mode lock); then the
database read fails here. Both node_gate and transport_guard must refuse -
nothing sent. The cached value is used only when the mode-state file proves
it current under the shared lock (no change since); a MISSING state file
proves nothing (never "epoch 0"), so a cold process, a removed state file
(even after a change elsewhere to 'enforced') or an unusable lock file
refuses on a failed read.
"""
import contextlib
import datetime as dt
import os
import subprocess
import sys
import tempfile
import textwrap
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp
from app.services import gate_locks, node_gate, provisioning_schema, receipt_void_schema, transport_guard
from app.services.mikrotik_client import MikrotikClient

failures = []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The other process: commits 'enforced' under the EXCLUSIVE gate-mode lock,
# exactly in the order of provisioning_control.change_gate_mode, then exits.
CHANGE = textwrap.dedent("""
    import sys
    sys.path.insert(0, sys.argv[1])
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import models_provisioning as mp
    from app.services import gate_locks
    url, installation, base, target = sys.argv[2:6]
    engine = create_engine(url)
    with gate_locks.FileLock(gate_locks.mode_lock_path(installation, base)).acquire(shared=False, timeout=10):
        gate_locks.write_mode_state(installation, gate_locks.MODE_STATE_CHANGING, base)
        with sessionmaker(bind=engine)() as db:
            row = db.get(mp.ProvisioningRuntimeState, 1)
            row.gate_mode, row.gate_mode_epoch = target, row.gate_mode_epoch + 1
            db.commit()
            epoch = row.gate_mode_epoch
        gate_locks.write_mode_state(installation, str(epoch), base)
    engine.dispose()
    print("committed", flush=True)
""")


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


def _broken_factory():
    raise RuntimeError("database is down")


def scenario(label, engine, lock_dir, url):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Factory)["ready"],
                                provisioning_schema.bootstrap(engine, Factory)["ready"]), (True, True))
    with Factory() as db:
        node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.19.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.82.0.0/24")
        db.add(node)
        row = db.get(mp.ProvisioningRuntimeState, 1)
        now = dt.datetime.utcnow()
        row.owner_state, row.owner_host_id, row.owner_boot_id = "active", "c3" * 32, str(uuid.uuid4())
        row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = now
        row.ownership_epoch, row.lock_backend = 1, "flock"
        db.commit()
        db.refresh(node)
        db.expunge(node)
        installation = row.installation_uuid
    os.makedirs(gate_locks.installation_dir(installation, lock_dir), mode=0o700, exist_ok=True)

    def write():
        """One uninstrumented legacy RouterOS write: sent | refused."""
        client = MikrotikClient.for_node(node)
        raw = FakeRouteros()
        client._api = transport_guard.RouterosApi(raw, client)
        try:
            client._api.path("ip", "address").add(address="10.99.0.1/32")
            return "sent" if raw.sent else "nothing"
        except transport_guard.GateViolation:
            return "refused" if not raw.sent else "refused-after-send"

    def passage():
        try:
            with node_gate.writer_gate(node) as context:
                return f"entered:{context.mode}"
        except node_gate.GateNotAvailable:
            return "refused"

    def down():
        return patch("app.database.SessionLocal", _broken_factory)

    def cache_off():
        node_gate.reset_cache()
        check(L + "the mode 'off' is read and cached", (passage(), node_gate._cached_runtime.gate_mode),
              ("entered:off", "off"))
        node_gate._checked_at = None              # the next passage must try a (failing) read

    state_file = gate_locks.mode_state_path(installation, lock_dir)

    # ---------------------------------------------------------------- no change: proven only by the file
    cache_off()
    check(L + "no change was ever made: there is no mode-state file", os.path.exists(state_file), False)
    with down():
        check(L + "database down, no state file: node_gate refuses (a missing file is never epoch 0)",
              passage(), "refused")
        node_gate._checked_at = None
        check(L + "...transport_guard refuses, nothing sent", write(), "refused")
    gate_locks.write_mode_state(installation, "0", lock_dir)
    cache_off()
    with down():
        check(L + "database down, the state file names the cached epoch: node_gate acts on the proven 'off'",
              passage(), "entered:off")
        check(L + "...transport_guard too", write(), "sent")

    # ---------------------------------------------------------------- another process: off -> enforced
    cache_off()
    changer = subprocess.run([sys.executable, "-c", CHANGE, BACKEND, url, installation, lock_dir, "enforced"],
                             env=dict(os.environ, UM_LOCK_DIR=lock_dir), capture_output=True, text=True,
                             timeout=120)
    check(L + "another process committed 'enforced' and released the lock", changer.stdout.strip(), "committed")
    check(L + "...this process still has 'off' cached", node_gate._cached_runtime.gate_mode, "off")
    with down():
        check(L + "database down after that change: node_gate refuses (the epoch moved)", passage(), "refused")
        node_gate._checked_at = None
        check(L + "...an uninstrumented write is refused, nothing sent", write(), "refused")
    os.remove(state_file)                         # the state file is gone after that change
    with down():
        node_gate._checked_at = None
        check(L + "cache off/epoch 0, 'enforced' committed elsewhere, state file removed, database down: "
              "node_gate refuses", passage(), "refused")
        node_gate._checked_at = None
        check(L + "...transport_guard refuses, nothing sent", write(), "refused")
    with Factory() as db:
        check(L + "the persisted mode really is 'enforced'", db.get(mp.ProvisioningRuntimeState, 1).gate_mode,
              "enforced")
    check(L + "database back: the write is refused by the fresh 'enforced'", write(), "refused")

    # ---------------------------------------------------------------- a change caught half-way
    with Factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.gate_mode = "off"
        db.commit()
        epoch = row.gate_mode_epoch
    gate_locks.write_mode_state(installation, str(epoch), lock_dir)
    cache_off()
    gate_locks.write_mode_state(installation, gate_locks.MODE_STATE_CHANGING, lock_dir)   # crashed mid-change
    with down():
        check(L + "database down while the state file says 'changing': node_gate refuses", passage(), "refused")
        check(L + "...transport_guard refuses", write(), "refused")
    gate_locks.write_mode_state(installation, str(epoch), lock_dir)

    # ---------------------------------------------------------------- no usable lock, or a cold process
    cache_off()
    blocker = os.path.join(lock_dir, "not-a-directory")
    open(blocker, "a").close()
    with down(), patch.dict(os.environ, {"UM_LOCK_DIR": os.path.join(blocker, "locks")}):
        check(L + "database down AND the lock file unusable: node_gate refuses", passage(), "refused")
        check(L + "...transport_guard refuses", write(), "refused")
    node_gate.reset_cache()
    with down():
        check(L + "a cold process (nothing ever read) with the database down: node_gate refuses",
              passage(), "refused")
        node_gate.reset_cache()
        check(L + "...transport_guard refuses", write(), "refused")
    node_gate.reset_cache()
    check(L + "database back: an 'off' write is sent again (today's behaviour)", write(), "sent")


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="gate_read_"))

    def run(label, engine, url):
        lock_dir = os.path.join(folder, f"locks-{label}")
        os.mkdir(lock_dir, 0o700)
        with patch.dict(os.environ, {"UM_LOCK_DIR": lock_dir}), \
                patch("app.database.SessionLocal", sessionmaker(bind=engine, autoflush=False)):
            node_gate.reset_cache()
            scenario(label, engine, lock_dir, url)
            node_gate.reset_cache()

    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)
    run("sqlite", sqlite, sqlite.url.render_as_string(hide_password=False))
    maria_url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if maria_url:
        maria = scratch.claim(maria_url)
        cleanup.callback(scratch.release, maria)
        run("mariadb", maria, maria.url.render_as_string(hide_password=False))
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
