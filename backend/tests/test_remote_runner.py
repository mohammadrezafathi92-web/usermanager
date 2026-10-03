"""Lifecycle/P6 batch L1a-1 - the killable mutation runner (design v7.1,
sections 10.8 and 10.9). Run:
    python3 backend/tests/test_remote_runner.py

Only the runner's self-test actions exist in this batch, so everything
here runs without a node, a network or a database. Proven:

- a result comes back typed, for the right action (LP-162)
- the DTO's secrets reach the child only through the pipe: never argv,
  never the environment (LP-159) - checked from inside the child too
- past the hard deadline the child is SIGKILLed, its death is confirmed,
  the outcome is killed_unknown, and the node lock it held is free again
  with nobody having "released" it (LP-144, LP-145, LP-146, LP-161)
- a child that dies mid-action is killed_unknown as well
- the child itself takes and holds the node lock for the whole action
- an unregistered action never spawns anything
- a child whose parent is already gone exits before reading a DTO (LP-160)
- the parent process opens no application database for any of this
"""
from __future__ import annotations

import ast
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-runner-test-")

from app.services import gate_locks, remote_runner as rr
from app.services.remote_action import ActionType, Outcome, RemoteActionDTO
from app.services.remote_runner_registry import ACTIONS

failures: list[str] = []
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES_DIR = os.path.join(BACKEND_DIR, "app", "services")
INSTALLATION = str(uuid.uuid4())
WORK = tempfile.mkdtemp(prefix="um-runner-markers-")
SECRET = "S3CR3T-runner-marker-51b7"


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


def dto(action, node_id=None, deadline=20, **over):
    kwargs = dict(action_type=action, node_id=node_id, timeouts={"hard_deadline": deadline},
                  fencing={"installation_uuid": INSTALLATION} if node_id else {})
    kwargs.update(over)
    return RemoteActionDTO(**kwargs)


def node_lock_is_free(node_id) -> bool:
    try:
        gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, node_id)).acquire(shared=False, timeout=0).release()
        return True
    except gate_locks.LockTimeout:
        return False


def pid_alive(pid) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_for(path, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return True
        time.sleep(0.02)
    return False


print("--- a normal round trip ---")
request = dto(ActionType.SELFTEST_ECHO, node_id=4, params={"hello": [1, "two", None]})
result = rr.run_action(request)
check("outcome is succeeded and the params came back", (result.outcome, result.observed["params"]),
      (Outcome.SUCCEEDED, {"hello": [1, "two", None]}))
check("the result is for THIS action", result.action_id, request.action_id)
check("the action ran in another process", result.observed["pid"] != os.getpid())
check("duration is reported", result.duration_ms >= 0)
check("a succeeded result is not 'unknown'", result.is_unknown, False)
check("the child released the node lock on exit", node_lock_is_free(4))

print("--- secrets travel only through the pipe (LP-159) ---")
secret_request = dto(ActionType.SELFTEST_ENVIRONMENT, node_id=4,
                     credential={"xr_uuid": SECRET}, node_secrets={"password": SECRET + "-node"})
command = rr.child_command(os.getpid())
check("the child's argv is the interpreter, -m, the module and the parent pid - nothing else",
      command[1:], ["-m", "app.services.remote_runner_child", str(os.getpid())])
check("no secret in that argv", any(SECRET in part for part in command), False)
seen = rr.run_action(secret_request)
check("the child received both secrets", seen.observed["secrets_received"], 2)
check("...and, looking at itself, finds none in its argv", seen.observed["secret_in_argv"], False)
check("...and none in its environment", seen.observed["secret_in_env"], False)
check("the parent's own environment was not modified to carry them",
      any(SECRET in value for value in os.environ.values()), False)
check("the result does not echo the secrets back", SECRET in seen.to_wire(), False)

print("--- hard deadline: SIGKILL, confirmed death, lock freed by the kernel (LP-144/145/146/161) ---")
marker = os.path.join(WORK, "sleep-started")
box = {}


def run_hanging():
    box["result"] = rr.run_action(dto(ActionType.SELFTEST_SLEEP, node_id=9, deadline=1.5,
                                      params={"seconds": 60, "started_marker": marker}))


thread = threading.Thread(target=run_hanging)
began = time.monotonic()
thread.start()
check("the hanging action started (inside the child, after taking the node lock)", wait_for(marker))
child_pid = int(open(marker).read())
check("while it hangs, the node lock is HELD - a second writer cannot enter", node_lock_is_free(9), False)
check("...and waiting does not free it (no TTL)",
      raises(gate_locks.LockTimeout, lambda: gate_locks.FileLock(
          gate_locks.node_lock_path(INSTALLATION, 9)).acquire(shared=False, timeout=0.4)))
thread.join(20)
took = time.monotonic() - began
killed = box["result"]
check("outcome is killed_unknown", (killed.outcome, killed.error_code), (Outcome.KILLED_UNKNOWN, "hard_deadline_exceeded"))
check("which is 'unknown' - not success, not failure", killed.is_unknown, True)
check("it returned at the deadline, not after the 60s sleep", 1.4 < took < 10)
check("the child process is really dead", pid_alive(child_pid), False)
check("the node lock is free again - released by the kernel, by nobody's command", node_lock_is_free(9))
check("no API exists to release a runner's lock from outside",
      [n for n in dir(rr) if "release" in n.lower() or "unlock" in n.lower()], [])
after = rr.run_action(dto(ActionType.SELFTEST_ECHO, node_id=9, params={"after": "kill"}))
check("the next action on that node runs normally", after.outcome, Outcome.SUCCEEDED)

print("--- a child that dies mid-action ---")
crash_marker = os.path.join(WORK, "crash-started")
crashed = rr.run_action(dto(ActionType.SELFTEST_CRASH, node_id=11, params={"started_marker": crash_marker, "exit_code": 9}))
check("the action had started", os.path.exists(crash_marker))
check("no result -> killed_unknown, never 'succeeded' or a plain failure",
      (crashed.outcome, crashed.error_code, crashed.is_unknown), (Outcome.KILLED_UNKNOWN, "runner_exit_9", True))
check("its node lock is free", node_lock_is_free(11))

print("--- the node lock really serializes two runners ---")
order_marker = os.path.join(WORK, "first-started")
first_box = {}
first = threading.Thread(target=lambda: first_box.update(r=rr.run_action(
    dto(ActionType.SELFTEST_SLEEP, node_id=13, params={"seconds": 1.2, "started_marker": order_marker}))))
first.start()
wait_for(order_marker)
began = time.monotonic()
second = rr.run_action(dto(ActionType.SELFTEST_ECHO, node_id=13))
waited = time.monotonic() - began
first.join()
check("the second runner on the same node waited for the first", (second.outcome, waited > 0.8), (Outcome.SUCCEEDED, True))
began = time.monotonic()
other_node = rr.run_action(dto(ActionType.SELFTEST_ECHO, node_id=14))
check("a runner on ANOTHER node does not wait", other_node.outcome, Outcome.SUCCEEDED)

never_marker = os.path.join(WORK, "must-never-start")
blocker = gate_locks.FileLock(gate_locks.node_lock_path(INSTALLATION, 15)).acquire(shared=False, timeout=0)
timed_out = rr.run_action(dto(ActionType.SELFTEST_SLEEP, node_id=15, deadline=0.8,
                              params={"seconds": 0, "started_marker": never_marker}))
time.sleep(0.5)   # had the action been running after all, it would have written its marker by now
check("node lock held by someone else until the deadline: the result is not a success",
      (timed_out.outcome in (Outcome.TRANSPORT_ERROR, Outcome.KILLED_UNKNOWN), timed_out.outcome is Outcome.SUCCEEDED),
      (True, False))
check("...and the action provably never started: its marker does not exist", os.path.exists(never_marker), False)
check("...while the blocker still holds the lock (nothing took it from it)", node_lock_is_free(15), False)
blocker.release()
check("...and once the lock is free the same action does run and write its marker",
      (rr.run_action(dto(ActionType.SELFTEST_SLEEP, node_id=15,
                         params={"seconds": 0, "started_marker": never_marker})).outcome,
       os.path.exists(never_marker)),
      (Outcome.SUCCEEDED, True))

print("--- only registered actions, by name ---")
check("only the four self-test actions are registered in this batch (no real remote mutation)",
      sorted(a.value for a in ACTIONS),
      ["selftest_crash", "selftest_echo", "selftest_environment", "selftest_sleep"])
spawned = []
real_popen = subprocess.Popen


def counting_popen(*a, **k):
    spawned.append(a)
    return real_popen(*a, **k)


subprocess.Popen = counting_popen
try:
    check("an unregistered (real) action is refused",
          raises(rr.UnregisteredAction, lambda: rr.run_action(dto(ActionType.WG_ENSURE_PRESENT, node_id=1))))
    check("...before any process is spawned", spawned, [])
    check("something that is not a DTO is refused", raises(TypeError, lambda: rr.run_action({"action_type": "selftest_echo"})))
    check("...also before any spawn", spawned, [])
finally:
    subprocess.Popen = real_popen
check("every registered implementation is a 'module:function' string, not a callable",
      all(isinstance(v, str) and v.count(":") == 1 for v in ACTIONS.values()))

print("--- the child, driven directly: protocol violations exit before any action ---")
HEADER = struct.Struct(">Q")


def raw_child(parent_pid, stdin_bytes):
    process = subprocess.run([sys.executable, "-m", "app.services.remote_runner_child", str(parent_pid)],
                             input=stdin_bytes, capture_output=True, cwd=BACKEND_DIR, timeout=30)
    return process.returncode, process.stdout


def framed(text: str) -> bytes:
    payload = text.encode("utf-8")
    return HEADER.pack(len(payload)) + payload


orphan_marker = os.path.join(WORK, "orphan-ran")
orphan = dto(ActionType.SELFTEST_SLEEP, params={"seconds": 0, "started_marker": orphan_marker})
code, out = raw_child(os.getpid() + 1_000_000, framed(orphan.to_wire()))
check("LP-160: expected parent is not the real parent -> exit 3, nothing written", (code, out), (3, b""))
check("...and the action never ran", os.path.exists(orphan_marker), False)
check("no parent pid argument -> exit 3",
      subprocess.run([sys.executable, "-m", "app.services.remote_runner_child"], input=b"", capture_output=True,
                     cwd=BACKEND_DIR, timeout=30).returncode, 3)
me = os.getpid()
check("empty stdin -> exit 4", raw_child(me, b"")[0], 4)
check("a truncated message -> exit 4", raw_child(me, framed(orphan.to_wire())[:-5])[0], 4)
check("not JSON -> exit 5", raw_child(me, framed("\x80\x04 not json"))[0], 5)
import json  # noqa: E402

wire = json.loads(orphan.to_wire())
check("LP-162: another schema_version -> exit 5, action not run",
      (raw_child(me, framed(json.dumps({**wire, "schema_version": 999})))[0], os.path.exists(orphan_marker)), (5, False))
check("an unknown action_type -> exit 5", raw_child(me, framed(json.dumps({**wire, "action_type": "os_system"})))[0], 5)
check("a known but UNREGISTERED action_type -> exit 5",
      raw_child(me, framed(json.dumps({**wire, "action_type": "wg_ensure_present", "node_id": 1})))[0], 5)
check("an extra field -> exit 5", raw_child(me, framed(json.dumps({**wire, "target": "os:system"})))[0], 5)
check("a DTO fenced for another parent pid -> exit 5",
      raw_child(me, framed(json.dumps({**wire, "fencing": {"expected_parent_pid": me + 1}})))[0], 5)
code, out = raw_child(me, framed(orphan.to_wire()))
check("the same DTO, intact, runs and answers", (code, len(out) > HEADER.size, os.path.exists(orphan_marker)), (0, True, True))

print("--- parent-death guard: fail-closed, before any byte of the DTO is read ---")
import signal  # noqa: E402

from app.services import remote_runner_child as child  # noqa: E402

SIGKILL = int(signal.SIGKILL)


def fake_prctl(set_result=0, get_result=0, readback=SIGKILL, boom=None):
    import ctypes

    def prctl(option, arg2, *rest):
        if boom:
            raise boom
        if option == child.PR_SET_PDEATHSIG:
            return set_result
        if option == child.PR_GET_PDEATHSIG:
            ctypes.c_int.from_address(arg2).value = readback
            return get_result
        return -1
    return prctl


check("Linux, prctl succeeds and reads back SIGKILL -> armed",
      child.arm_parent_death_signal("linux", fake_prctl()), True)
check("Linux, PR_SET_PDEATHSIG returns -1 -> NOT armed",
      child.arm_parent_death_signal("linux", fake_prctl(set_result=-1)), False)
check("Linux, the read-back call fails -> NOT armed",
      child.arm_parent_death_signal("linux", fake_prctl(get_result=-1)), False)
check("Linux, the read-back shows another signal -> NOT armed",
      child.arm_parent_death_signal("linux", fake_prctl(readback=int(signal.SIGTERM))), False)
check("Linux, the read-back shows no signal at all -> NOT armed",
      child.arm_parent_death_signal("linux", fake_prctl(readback=0)), False)
check("Linux, prctl raises -> NOT armed (never an exception out of the guard)",
      child.arm_parent_death_signal("linux", fake_prctl(boom=OSError("EPERM"))), False)
check("not Linux: nothing to arm, reported as such", child.arm_parent_death_signal("darwin", fake_prctl(set_result=-1)), True)
if sys.platform.startswith("linux"):
    check("on this Linux host the real prctl arms and confirms the signal", child.arm_parent_death_signal(), True)


class _RecordingStdin:
    def __init__(self):
        self.reads = 0
        self.buffer = self

    def read(self, *a):
        self.reads += 1
        return b""


guard_marker = os.path.join(WORK, "guard-ran")
real_arm, real_stdin = child.arm_parent_death_signal, sys.stdin
recording = _RecordingStdin()
child.arm_parent_death_signal = lambda: False
sys.stdin = recording
try:
    exit_code = child.main(["remote_runner_child", str(os.getppid())])
finally:
    child.arm_parent_death_signal, sys.stdin = real_arm, real_stdin
check("guard not armed -> main() exits 3", exit_code, 3)
check("...without reading a single byte of stdin (no DTO, no secret)", recording.reads, 0)
check("...and no action ran", os.path.exists(guard_marker), False)
source = open(os.path.join(SERVICES_DIR, "remote_runner_child.py"), encoding="utf-8").read()
main_body = source[source.index("def main("):]
check("in main(), arming comes before the parent-pid check, which comes before reading the DTO",
      main_body.index("arm_parent_death_signal()") < main_body.index("os.getppid()") < main_body.index("_read_message("))
check("the prctl wrapper declares argtypes and restype", ("prctl.argtypes" in source, "prctl.restype" in source), (True, True))

print("--- malformed answers are killed_unknown, never trusted ---")
real_command = rr.child_command


def fake_child(script):
    return lambda parent_pid: [sys.executable, "-c", script]


good_id = str(uuid.uuid4())
answers = {
    "exit 0 with no output": "import sys; sys.stdin.buffer.read()",
    "garbage instead of a framed message": "import sys; sys.stdin.buffer.read(); sys.stdout.buffer.write(b'x'*64)",
    "a framed message that is not a result":
        "import sys,struct; sys.stdin.buffer.read(); p=b'{\"hello\":1}'; "
        "sys.stdout.buffer.write(struct.pack('>Q',len(p))+p)",
    "a valid result for ANOTHER action":
        "import sys,struct,json; sys.stdin.buffer.read(); "
        f"p=json.dumps({{'action_id':'{good_id}','outcome':'succeeded','schema_version':1}}).encode(); "
        "sys.stdout.buffer.write(struct.pack('>Q',len(p))+p)",
    "a result of another schema version":
        "import sys,struct,json; d=json.loads(sys.stdin.buffer.read()[8:]); "
        "p=json.dumps({'action_id':d['action_id'],'outcome':'succeeded','schema_version':2}).encode(); "
        "sys.stdout.buffer.write(struct.pack('>Q',len(p))+p)",
    "a length header that does not match the body":
        "import sys,struct; sys.stdin.buffer.read(); sys.stdout.buffer.write(struct.pack('>Q',999)+b'{}')",
}
try:
    for label, script in answers.items():
        rr.child_command = fake_child(script)
        answer = rr.run_action(dto(ActionType.SELFTEST_ECHO))
        check(f"{label} -> killed_unknown", (answer.outcome, answer.is_unknown), (Outcome.KILLED_UNKNOWN, True))
finally:
    rr.child_command = real_command

print("--- the runner modules stay away from the application database (LP-157, static half) ---")
FORBIDDEN = ("database", "models", "models_provisioning", "provisioning_schema", "sqlalchemy", "node_gate")
for module in ("remote_runner_child", "remote_runner_selftest", "remote_runner_registry", "remote_action",
               "gate_locks", "remote_runner"):
    tree = ast.parse(open(os.path.join(SERVICES_DIR, f"{module}.py"), encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[-1] for alias in node.names} | {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[-1])
            imported.add((node.module or "").split(".")[0])
            imported |= {alias.name for alias in node.names}
    check(f"{module}.py imports no database/model/session module", sorted(imported & set(FORBIDDEN)), [])
    check(f"{module}.py does not use pickle or marshal", sorted(imported & {"pickle", "marshal", "shelve", "dill"}), [])
loaded = subprocess.run(
    [sys.executable, "-c",
     "import sys, app.services.remote_runner_child, app.services.remote_runner_selftest; "
     "print(sorted(m for m in sys.modules if m.startswith('sqlalchemy') or m in "
     "('app.database','app.models','app.models_provisioning')))"],
    capture_output=True, text=True, cwd=BACKEND_DIR, timeout=30)
check("importing the child loads neither SQLAlchemy nor any application model", loaded.stdout.strip(), "[]")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
