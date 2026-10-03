"""Lifecycle/P6 batch L1a-1 - node_gate and its lock primitives, with the
emphasis on 'off': ZERO behavioural change.

Design v7.1, sections 10.2 and 10.6. Run:
    python3 backend/tests/test_node_gate_off_mode.py

In 'off' node_gate must take exactly ONE lock - the shared gate-mode lock -
and nothing else: no node lock, no serialization between two writers on
the same node, no waiting on another writer, and never an error, even when
the lock directory is unusable or the provisioning schema is not ready
(LP-133, LP-168 for the gate itself; the writers are not routed through it
in this batch - see tests/test_remote_writers_inventory.py).
"""
from __future__ import annotations

import fcntl
import os
import stat
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
LOCK_DIR = tempfile.mkdtemp(prefix="um-gate-test-")
os.environ["UM_LOCK_DIR"] = LOCK_DIR

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models_provisioning as mp
from app.services import gate_locks, node_gate as ng, provisioning_schema as ps

failures: list[str] = []


def exclusive_is_free(lock_path, timeout=0) -> bool:
    """Takes the exclusive lock and gives it straight back."""
    try:
        gate_locks.FileLock(lock_path).acquire(shared=False, timeout=timeout).release()
        return True
    except gate_locks.LockTimeout:
        return False


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


# A FILE database: with "sqlite://" every thread's connection would get its
# own empty in-memory database, and the writer threads below would see no
# provisioning tables at all.
DB_FILE = os.path.join(tempfile.mkdtemp(prefix="um-gate-db-"), "gate.db")
engine = create_engine(f"sqlite:///{DB_FILE}", connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
check("provisioning schema bootstraps (the singleton is the gate's source of truth)",
      ps.bootstrap(engine, Session)["ready"], True)
db = Session()
INSTALLATION = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
db.close()


def set_runtime(**values):
    db = Session()
    row = db.get(mp.ProvisioningRuntimeState, 1)
    for key, value in values.items():
        setattr(row, key, value)
    db.commit()
    db.close()


class FlockSpy:
    """Records every flock() the gate makes, by lock-file name and kind."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._real = fcntl.flock

    def __enter__(self):
        def spy(fd, operation):
            try:                                   # Linux
                name = os.path.basename(os.readlink(f"/proc/self/fd/{fd}"))
            except OSError:                        # macOS
                raw = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
                name = os.path.basename(raw.split(b"\0")[0].decode())
            kind = "SH" if operation & fcntl.LOCK_SH else "EX" if operation & fcntl.LOCK_EX else "UN"
            self.calls.append((name, kind))
            return self._real(fd, operation)
        gate_locks.fcntl.flock = spy
        return self

    def __exit__(self, *exc):
        gate_locks.fcntl.flock = self._real


print("--- lock primitives ---")
path = gate_locks.mode_lock_path(INSTALLATION)
check("mode lock path is {lock_dir}/{installation_uuid}/gate-mode.lock",
      path, os.path.join(LOCK_DIR, INSTALLATION, "gate-mode.lock"))
check("node lock path is {lock_dir}/{installation_uuid}/node-{id}.lock",
      gate_locks.node_lock_path(INSTALLATION, 12), os.path.join(LOCK_DIR, INSTALLATION, "node-12.lock"))
for bad in ("..", "../x", "not-a-uuid", INSTALLATION.upper(), "", INSTALLATION + "/.."):
    check(f"installation uuid {bad!r} cannot become a path", raises(ValueError, lambda b=bad: gate_locks.mode_lock_path(b)))
for bad in (0, -1, True, "3", 3.0, None):
    check(f"node id {bad!r} cannot become a path", raises(ValueError, lambda b=bad: gate_locks.node_lock_path(INSTALLATION, b)))

a = gate_locks.FileLock(path).acquire(shared=True, timeout=0)
b = gate_locks.FileLock(path).acquire(shared=True, timeout=0)
check("two shared holds coexist (even in one process: each hold has its own descriptor)", (a.held, b.held), (True, True))
check("an exclusive hold is refused while a shared one exists",
      raises(gate_locks.LockTimeout, lambda: gate_locks.FileLock(path).acquire(shared=False, timeout=0.1)))
a.release()
b.release()
ex = gate_locks.FileLock(path).acquire(shared=False, timeout=0)
check("a shared hold is refused while the exclusive one exists",
      raises(gate_locks.LockTimeout, lambda: gate_locks.FileLock(path).acquire(shared=True, timeout=0.1)))
check("a second exclusive hold is refused too",
      raises(gate_locks.LockTimeout, lambda: gate_locks.FileLock(path).acquire(shared=False, timeout=0)))
ex.release()
check("release frees it", exclusive_is_free(path, 0))
held_once = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, 99)).acquire(shared=True, timeout=0)
check("a held FileLock cannot be acquired twice", raises(RuntimeError, lambda: held_once.acquire(shared=True, timeout=0)))
held_once.release()
check("lock files are created 0600",
      stat.S_IMODE(os.stat(path).st_mode) & 0o077, 0)
check("FileLock has no method that could release another holder's lock",
      sorted(n for n in dir(gate_locks.FileLock) if not n.startswith("_")), ["acquire", "held", "release"])

print("--- GET_LOCK name (LP-149) ---")
name = gate_locks.get_lock_name(INSTALLATION, 1)
check("always 60 characters, under GET_LOCK's 64", (len(name), len(gate_locks.get_lock_name(INSTALLATION, 2**31 - 1))), (60, 60))
check("only [a-z0-9:]", set(name) <= set("abcdefghijklmnopqrstuvwxyz0123456789:"))
other = str(uuid.uuid4())
check("another installation, same node -> another name (LP-147)", gate_locks.get_lock_name(other, 1) != name)
check("same installation, another node -> another name", gate_locks.get_lock_name(INSTALLATION, 2) != name)
check("same installation and node -> the same name (LP-148)", gate_locks.get_lock_name(INSTALLATION, 1), name)

print("--- off: exactly one shared lock, nothing else ---")
ng.counters.reset()
with FlockSpy() as spy:
    with ng.node_gate(5, session_factory=Session) as ctx:
        inside = ng.current_context()
check("the block runs and gets a ShadowGateContext(mode='off')",
      (type(ctx).__name__, ctx.mode, ctx.node_id, ctx.mode_lock_held, ctx.node_lock_held),
      ("ShadowGateContext", "off", 5, True, False))
check("current_context() is that context inside, None outside", (inside is ctx, ng.current_context()), (True, None))
check("exactly one flock call: LOCK_SH on gate-mode.lock", spy.calls, [("gate-mode.lock", "SH")])
check("no node lock file was even created", os.path.exists(gate_locks.node_lock_path(INSTALLATION, 5)), False)
check("the shared lock is released afterwards (an exclusive hold is immediately possible)",
      exclusive_is_free(path, 0))
check("counted as one passage, no miss, no contention", ng.counters.snapshot(),
      {"mode_lock_miss": 0, "contention": 0, "passages": 1})

print("--- off: two writers on the SAME node are not serialized (LP-168) ---")
barrier = threading.Barrier(2, timeout=5)
overlap = []


def writer():
    with ng.node_gate(5, session_factory=Session):
        try:
            barrier.wait()          # both threads must be INSIDE the gate at once
            overlap.append(True)
        except threading.BrokenBarrierError:
            overlap.append(False)


threads = [threading.Thread(target=writer) for _ in range(2)]
started = time.monotonic()
for t in threads:
    t.start()
for t in threads:
    t.join()
check("both writers were inside node_gate(5) at the same moment", overlap, [True, True])
check("...without waiting on each other", time.monotonic() - started < 2.0)

print("--- off: nested gates are allowed, for any node ---")
with ng.node_gate(5, session_factory=Session) as outer:
    with ng.node_gate(6, session_factory=Session) as inner:
        check("inner context is the current one", ng.current_context() is inner)
    check("outer context is restored", ng.current_context() is outer)

print("--- off: an exception in the block propagates and still releases ---")
def _raise_inside():
    with ng.node_gate(5, session_factory=Session):
        raise KeyError("boom")


check("the block's exception propagates unchanged", raises(KeyError, _raise_inside))
check("...and the lock is released", exclusive_is_free(path, 0))
check("...and the context is cleared", ng.current_context(), None)

print("--- off NEVER fails or blocks because of the gate ---")
ng.counters.reset()
saved_dir = os.environ["UM_LOCK_DIR"]
blocker = tempfile.NamedTemporaryFile(delete=False)
os.environ["UM_LOCK_DIR"] = os.path.join(blocker.name, "cannot-be-a-directory")
ran = []
with ng.node_gate(5, session_factory=Session) as ctx:
    ran.append(ctx.mode_lock_held)
check("unusable lock directory: the block still runs, without the lock", ran, [False])
check("...and the miss is counted", ng.counters.snapshot()["mode_lock_miss"], 1)
os.environ["UM_LOCK_DIR"] = saved_dir

ng.counters.reset()
ps.mark_not_ready("simulated: schema not verified")
with ng.node_gate(5, session_factory=Session) as ctx:
    check("schema not ready: the block still runs as 'off', without the lock", (ctx.mode, ctx.mode_lock_held), ("off", False))
check("...and the miss is counted", ng.counters.snapshot()["mode_lock_miss"], 1)


def _broken_factory():
    raise RuntimeError("database is down")


ps.bootstrap(engine, Session)
with ng.node_gate(5, session_factory=_broken_factory) as ctx:
    check("database down: the block still runs as 'off'", (ctx.mode, ctx.mode_lock_held), ("off", False))

ng.counters.reset()
changing = gate_locks.FileLock(path).acquire(shared=False, timeout=0)   # a mode change in progress
baseline_began = time.monotonic()
for _ in range(20):
    with ng.node_gate(5, session_factory=Session) as ctx:
        pass
blocked_elapsed = time.monotonic() - baseline_began
check("a mode change holding the lock: the 'off' writer proceeds WITHOUT the lock", ctx.mode_lock_held, False)
check("...immediately: 20 passages while the lock is held exclusively take well under a second "
      f"({blocked_elapsed * 1000:.0f} ms)", blocked_elapsed < 1.0)
changing.release()
free_began = time.monotonic()
for _ in range(20):
    with ng.node_gate(5, session_factory=Session):
        pass
free_elapsed = time.monotonic() - free_began
check("...and no slower than 20 passages with the lock free, give or take scheduling noise "
      f"({free_elapsed * 1000:.0f} ms)", blocked_elapsed < free_elapsed + 0.5)
check("...each of the 20 counted as a miss", ng.counters.snapshot()["mode_lock_miss"], 20)
check("the gate has no wait constant left on the writer path", hasattr(ng, "MODE_LOCK_WAIT_SECONDS"), False)
with FlockSpy() as spy:
    with ng.node_gate(5, session_factory=Session):
        pass
check("the single flock is a non-blocking attempt (the primitive always ORs LOCK_NB)", spy.calls, [("gate-mode.lock", "SH")])

print("--- a mode change waits for writers that started before it (LP-165, LP-167 mechanism) ---")
inside_gate, let_go = threading.Event(), threading.Event()


def slow_writer():
    with ng.node_gate(5, session_factory=Session):
        inside_gate.set()
        let_go.wait(5)


thread = threading.Thread(target=slow_writer)
thread.start()
inside_gate.wait(5)
check("while an 'off' writer is inside, the exclusive mode lock cannot be taken, however long it waits",
      raises(gate_locks.LockTimeout, lambda: gate_locks.FileLock(path).acquire(shared=False, timeout=0.3)))
let_go.set()
thread.join()
check("once that writer left, it can", exclusive_is_free(path, 1))

print("--- invalid use ---")
for bad in (0, -3, None, "5", True, 5.0):
    check(f"node_gate({bad!r}) is rejected (a saved node id is required)",
          raises(ValueError, lambda b=bad: ng.node_gate(b, session_factory=Session).__enter__()))

print("--- shadow: measures, never blocks, never claims exclusivity ---")
set_runtime(lock_backend="flock", lock_verified_at=__import__("datetime").datetime.utcnow(), gate_mode="shadow",
            gate_mode_epoch=1)
ng.counters.reset()
with FlockSpy() as spy:
    with ng.node_gate(5, session_factory=Session) as first:
        with ng.node_gate(5, session_factory=Session) as second:
            pass
check("first writer happens to hold the node lock",
      (first.mode, first.node_lock_held, first.gate_mode_epoch), ("shadow", True, 1))
check("second writer on the same node proceeds WITHOUT it", (second.mode, second.node_lock_held), ("shadow", False))
check("contention counted once; both passages counted", ng.counters.snapshot(),
      {"mode_lock_miss": 0, "contention": 1, "passages": 2})
check("locks taken: shared mode lock + a non-blocking try of node-5.lock, per writer",
      spy.calls, [("gate-mode.lock", "SH"), ("node-5.lock", "EX"), ("gate-mode.lock", "SH"), ("node-5.lock", "EX")])
check("the context is still a ShadowGateContext - no exclusive token exists in this batch",
      type(first).__name__, "ShadowGateContext")

print("--- enforced: not implemented in this batch -> fail-closed ---")
set_runtime(gate_mode="enforced", owner_state="active", owner_host_id="a" * 64, owner_boot_id=str(uuid.uuid4()),
            owner_claimed_at=__import__("datetime").datetime.utcnow(),
            owner_heartbeat_at=__import__("datetime").datetime.utcnow())
ran = []


def _enforced():
    with ng.node_gate(5, session_factory=Session):
        ran.append(True)


check("node_gate raises GateNotAvailable", raises(ng.GateNotAvailable, _enforced))
check("...and the block did NOT run", ran, [])
check("...and no lock is left held", exclusive_is_free(path, 0))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
