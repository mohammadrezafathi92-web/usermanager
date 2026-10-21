"""The panel update worker itself (scripts/panel_update_runner.py), run for
real exactly as the worker container runs it (the digest-anchored bootstrap
Cmd), with a stand-in update.sh and compose binary. No Docker daemon, no
network, nothing deployed.

  - success only from update.sh's own UPDATE_OK for the target AND exit 0;
  - every frozen file, the docker/flock shims included, checked before
    anything runs; an extra file on the shims' PATH refused; update.sh runs
    from a private copy, not from the shared job folder;
  - update.sh's lock is the one the CLI holds;
  - a deadline for the whole run and SIGTERM both stop update.sh's whole
    process group and leave an UNKNOWN (interrupted) state, never "failed".
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import local_deploy, panel_safe_update as ps, self_update

failures: list[str] = []
TARGET = "a" * 40
OK_LINE = f"UPDATE_OK commit={TARGET}; modes preserved; license-server untouched"


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


temp = Path(tempfile.mkdtemp(prefix="panel-update-runner-"))
project, data = temp / "project", temp / "data"
project.mkdir()
data.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
# the stand-in for update.sh: same lock, same progress protocol
(project / "update.sh").write_text(f"""#!/bin/bash
set -u
mode=$(cat "$UM_UPDATE_PROJECT_DIR/mode")
echo "$0" > "$UM_UPDATE_PROJECT_DIR/ran"
exec 9>"$(git rev-parse --git-path usermanager-update.lock)"
flock -n 9 || {{ echo "STOP: another update is running" >&2; exit 1; }}
echo UPDATE_STAGE=preflight
docker compose version > /dev/null || exit 4
echo UPDATE_STAGE=build
case $mode in
  ok) echo "{OK_LINE}" ;;
  okfail) echo "{OK_LINE}"; exit 1 ;;
  noline) exit 0 ;;
  hang) sleep 300 & echo $! > "$UM_UPDATE_PROJECT_DIR/child.pid"; wait ;;
esac
""")
compose = temp / "docker-compose"
compose.write_text(f"#!/bin/sh\necho \"$0 $*\" >> {project}/compose.log\n")
compose.chmod(0o700)
host_id = temp / "host.id"
host_id.write_bytes(b"x" * 32)

local_deploy.HOST_PROJECT_DIR = str(project)
ps.DATA, ps.ROOT, ps.HOST_ID = data, data / "update-runs", host_id
local_deploy.ensure_docker_compose_cli = lambda: str(compose)
self_update.current_revision = lambda: {"commit": "old", "branch": "main", "dirty": False, "dirty_files": []}
bodies = []


def docker_request(method, path, body=None, timeout=15):
    if path.startswith("/containers/create"):
        bodies.append(body)
        return 201, {"Id": "c" * 64}
    return 204, {}


local_deploy._docker_request = docker_request
local_deploy._docker_api = lambda path: {"Image": "sha256:" + "1" * 64, "Mounts": [
    {"Type": "bind", "Destination": str(project), "Source": str(project), "RW": True},
    {"Type": "bind", "Destination": str(data), "Source": str(data), "RW": True},
    {"Type": "bind", "Destination": "/etc/usermanager", "Source": "/etc/usermanager", "RW": False}]}


def job(mode, deadline=ps.DEADLINE_SECONDS):
    import shutil
    shutil.rmtree(ps.ROOT, ignore_errors=True)
    for name in ("ran", "child.pid", "compose.log"):
        (project / name).unlink(missing_ok=True)
    (project / "mode").write_text(mode)
    ps.DEADLINE_SECONDS = deadline
    queued = ps.queue(TARGET, 1)
    return ps.ROOT / queued["job_id"], bodies[-1]["Cmd"]


def start(cmd):
    return subprocess.Popen(cmd, cwd=project, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def state(folder):
    return json.loads((folder / "state.json").read_text())


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # a zombie of a killed child is not running
    try:
        return "Z" not in subprocess.check_output(["ps", "-o", "stat=", "-p", str(pid)], text=True)
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------- success and failure
folder, cmd = job("ok")
code = start(cmd).wait(60)
ran_from = (project / "ran").read_text().strip()
check("UPDATE_OK for the target and exit 0: completed, updated", (code, state(folder)["status"],
      state(folder)["updated"], state(folder)["commit"]), (0, "completed", True, TARGET))
check("...update.sh ran from a private copy, not from the shared job folder",
      (ran_from.endswith("update.sh"), str(folder) in ran_from), (True, False))
check("...docker compose went through the verified shim to the frozen compose copy",
      str(folder) not in (project / "compose.log").read_text() and "version" in (project / "compose.log").read_text())
for mode in ("okfail", "noline"):
    folder, cmd = job(mode)
    start(cmd).wait(60)
    check(f"{mode}: failed, never reported updated", (state(folder)["status"], state(folder)["updated"]), ("failed", False))

# ---------------------------------------------------------------- frozen inputs
for label, tamper in (("bin/docker changed", lambda f: (f / "bin/docker").write_text("#!/bin/sh\nexit 0\n")),
                      ("bin/flock changed", lambda f: (f / "bin/flock").write_text("#!/bin/sh\nexit 0\n")),
                      ("an extra file on the shims' PATH (bin/git)", lambda f: (f / "bin/git").write_text("x")),
                      ("update.sh changed", lambda f: (f / "update.sh").write_text("echo hi\n")),
                      ("helper changed", lambda f: (f / "helper.py").write_text("x")),
                      ("compose changed", lambda f: (f / "compose").write_text("x"))):
    folder, cmd = job("ok")
    tamper(folder)
    start(cmd).wait(60)
    check(f"{label}: refused before anything ran", (state(folder)["status"], state(folder).get("error_code"),
          (project / "ran").exists()), ("failed", "frozen_input_changed", False))
folder, cmd = job("ok")
(folder / "runner.py").write_text("print('replaced')\n")
code = start(cmd).wait(60)
check("runner.py changed: the bootstrap refuses; update.sh never ran (state still queued)",
      (code != 0, state(folder)["status"], (project / "ran").exists()), (True, "queued", False))
folder, cmd = job("ok")
config = json.loads((folder / "config.json").read_text())
config["hashes"]["update.sh"] = "0" * 64
(folder / "config.json").write_text(json.dumps(config))
code = start(cmd).wait(60)
check("config.json changed (its digests): the bootstrap refuses; update.sh never ran",
      (code != 0, state(folder)["status"], (project / "ran").exists()), (True, "queued", False))

# ---------------------------------------------------------------- the CLI's lock
folder, cmd = job("ok")
fd = os.open(project / ".git" / "usermanager-update.lock", os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
start(cmd).wait(60)
os.close(fd)
check("update.sh's lock held from the CLI: this run stops on it (failed, nothing further)",
      (state(folder)["status"], (project / "compose.log").exists()), ("failed", False))

# ---------------------------------------------------------------- deadline and stop
folder, cmd = job("hang", deadline=3)
began = time.time()
code = start(cmd).wait(90)
pid = int((project / "child.pid").read_text())
check("deadline: the run is stopped and recorded interrupted (unknown), not failed",
      (state(folder)["status"], state(folder)["error_code"], time.time() - began < 60),
      ("interrupted", "deadline_exceeded", True))
check("...update.sh's children stopped too", alive(pid), False)
check("...the status reader reports it unknown and resolvable",
      (ps._status(folder.name)["status"], ps._status(folder.name)["resolvable"]), ("unknown", True))
folder, cmd = job("hang")
runner = start(cmd)
for _ in range(300):
    if (project / "child.pid").exists():
        break
    time.sleep(0.05)
time.sleep(0.3)
runner.send_signal(signal.SIGTERM)
runner.wait(90)
pid = int((project / "child.pid").read_text())
check("SIGTERM to the worker: update.sh's group stopped, recorded interrupted",
      (state(folder)["status"], state(folder)["error_code"], alive(pid)), ("interrupted", "runner_stopped", False))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
