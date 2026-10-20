"""The panel update button's queue (services/panel_safe_update.py): fake
Docker daemon, a real local git checkout, no network, nothing deployed.

  - status reads the worker's id from container.json (config.json stays the
    immutable, digest-anchored input) and follows the runner's state;
  - a worker container in "created" is not a failure; an ambiguous create or
    start is not "nothing happened"; an undecided state blocks a new update
    until an operator resolves it (which never rolls anything back);
  - the same request twice (double click, retry) queues one job;
  - the API: superadmin only, own password, a second confirmation and the
    full main SHA; no password in any job file or in the worker's command.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import get_db
from app.deps import get_current_admin
from app.routers import panel_settings
from app.security import hash_password
from app.services import local_deploy, panel_safe_update as ps, self_update
from app.services.local_deploy import DeployError

failures: list[str] = []
TARGET, OTHER = "a" * 40, "b" * 40
IMAGE = "sha256:" + "1" * 64
PASSWORD = "update-confirm-test-password"


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


temp = Path(tempfile.mkdtemp(prefix="panel-update-queue-"))
project, data = temp / "project", temp / "data"
project.mkdir()
data.mkdir()
subprocess.run(["git", "init", "-q", "-b", "main"], cwd=project, check=True)
(project / "update.sh").write_text("#!/bin/bash\nexit 0\n")
compose = temp / "docker-compose"
compose.write_text("#!/bin/sh\nexit 0\n")
compose.chmod(0o700)
host_id = temp / "host.id"
host_id.write_bytes(b"x" * 32)

local_deploy.HOST_PROJECT_DIR = str(project)
ps.DATA, ps.ROOT, ps.HOST_ID = data, data / "update-runs", host_id
local_deploy.ensure_docker_compose_cli = lambda: str(compose)
self_update.current_revision = lambda: {"commit": "old", "branch": "main", "dirty": False, "dirty_files": []}


class Daemon:
    """The Docker API as the queue sees it; `behaviour` decides each answer."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.calls, self.containers, self.behaviour = [], {}, {}
        self.created_bodies = []

    def api(self, path):
        self.calls.append(("GET", path))
        return {"Image": IMAGE, "Mounts": [
            {"Type": "bind", "Destination": str(project), "Source": str(project), "RW": True},
            {"Type": "bind", "Destination": str(data), "Source": "/host/data", "RW": True},
            {"Type": "bind", "Destination": "/etc/usermanager", "Source": "/etc/usermanager", "RW": False}]}

    def request(self, method, path, body=None, timeout=15):
        self.calls.append((method, path))
        answer = self.behaviour.get((method, path.split("?")[0].split("/")[-1]))
        if callable(answer):
            return answer(path, body)
        if method == "POST" and path.startswith("/containers/create"):
            self.created_bodies.append(body)
            cid = "c" * 64
            self.containers[cid] = {"status": "created", "labels": body["Labels"]}
            return 201, {"Id": cid}
        if method == "POST" and path.endswith("/start"):
            self.containers[path.split("/")[2]]["status"] = "running"
            return 204, {}
        if method == "GET" and path.startswith("/containers/json"):
            return 200, [{"Id": cid} for cid, c in self.containers.items()
                         if any(path.find(v) >= 0 for v in c["labels"].values())]
        if method == "GET" and path.endswith("/json"):
            container = self.containers.get(path.split("/")[2])
            return (404, {}) if container is None else (200, {"State": {"Status": container["status"]}})
        if method == "DELETE":
            container = self.containers.get(path.split("/")[2])
            if container and container["status"] == "running":
                return 409, {}
            self.containers.pop(path.split("/")[2], None)
            return 204, {}
        return 500, {"message": "unexpected"}


daemon = Daemon()
local_deploy._docker_api = daemon.api
local_deploy._docker_request = daemon.request


def fresh():
    import shutil
    shutil.rmtree(ps.ROOT, ignore_errors=True)
    daemon.reset()


def attempt(target=TARGET):
    try:
        return ps.queue(target, 1)
    except DeployError as exc:
        return f"refused: {exc}"


def write_state(job, **value):
    ps.save(ps.ROOT / job / "state.json", value)


def held_cli_lock():
    fd = os.open(project / ".git" / "usermanager-update.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


# ---------------------------------------------------------------- dispatch and status
fresh()
job = attempt()
jid = job["job_id"]
folder = ps.ROOT / jid
config = json.loads((folder / "config.json").read_text())
check("dispatch: the job is queued (not 'updated')", (job["status"], job["updated"]), ("queued", False))
check("...the worker's id is in container.json, config.json stays without it",
      (json.loads((folder / "container.json").read_text())["container_id"], "container_id" in config), ("c" * 64, False))
check("...the digests cover every frozen file, the shims included", sorted(config["hashes"]), sorted(ps.FROZEN))
check("...the worker runs the pinned image id, labelled with the job",
      (daemon.created_bodies[0]["Image"], daemon.created_bodies[0]["Labels"]), (IMAGE, {ps.LABEL: jid}))
daemon.calls.clear()
status = ps.status()
check("status: the runner container is read by the id in container.json",
      ("GET", f"/containers/{'c' * 64}/json") in daemon.calls, True)
check("...queued and running container: starting", status["status"], "starting")
write_state(jid, status="running", phase="build", started_at=1, updated=False)
check("...the runner's own phase", (ps.status()["status"], ps.status()["phase"]), ("running", "build"))
check("the same request again (double click / retry): the same job, no second worker",
      (attempt()["job_id"], len(daemon.created_bodies)), (jid, 1))
check("another target while one runs: refused", str(attempt(OTHER)).startswith("refused"), True)
write_state(jid, status="completed", phase="completed", updated=True, commit=TARGET)
check("completed only from the runner's final state, with its commit",
      (ps.status()["status"], ps.status()["updated"]), ("completed", True))

# ---------------------------------------------------------------- the runner gone mid-way
fresh()
jid = attempt()["job_id"]
write_state(jid, status="running", phase="backend", started_at=1, updated=False)
daemon.containers.clear()                                       # removed (AutoRemove) without a final state
status = ps.status()
check("runner gone while update.sh ran: unknown, resolvable", (status["status"], status["error_code"],
      status["resolvable"]), ("unknown", "runner_lost", True))
check("...a new update is refused", str(attempt(OTHER)).startswith("refused"), True)
fd = held_cli_lock()
try:
    ps.resolve(jid, 1)
    outcome = "resolved"
except DeployError:
    outcome = "refused"
os.close(fd)
check("...resolve refused while update.sh's CLI lock is held", (outcome, ps.status()["status"]), ("refused", "unknown"))
resolved = ps.resolve(jid, 1)
check("...resolved: abandoned (nothing rolled back), a new update allowed",
      (resolved["status"], attempt(OTHER)["status"]), ("abandoned", "queued"))
fresh()
jid = attempt()["job_id"]
write_state(jid, status="interrupted", phase="build", error_code="deadline_exceeded")
check("runner stopped at its deadline: unknown, not failed", (ps.status()["status"], ps.status()["error_code"]),
      ("unknown", "deadline_exceeded"))
fresh()
jid = attempt()["job_id"]
daemon.containers.clear()                                       # the bootstrap refused: never wrote "running"
check("never past the bootstrap (still queued, container gone): failed, update.sh never ran",
      (ps.status()["status"], ps.status()["error_code"]), ("failed", "not_started"))

# ---------------------------------------------------------------- created / ambiguous dispatch
fresh()
daemon.behaviour[("POST", "start")] = lambda path, body: (_ for _ in ()).throw(TimeoutError())
daemon.behaviour[("DELETE", "c" * 64)] = lambda path, body: (500, {})
result = attempt()
jid = json.loads((ps.ROOT / "active.json").read_text())["job_id"]
status = ps.status()
check("start timed out, worker still 'created', not removable: unknown, NOT 'nothing changed'",
      ("نامعلوم" in str(result), status["status"], status["resolvable"]), (True, "starting", True))
check("...'created' is not a failure; the state is not marked failed",
      json.loads((ps.ROOT / jid / "state.json").read_text())["status"], "queued")
check("...a new update is refused", str(attempt(OTHER)).startswith("refused"), True)
del daemon.behaviour[("DELETE", "c" * 64)]
check("...resolve removes the never-started worker: failed, not started",
      (ps.resolve(jid, 1)["status"], daemon.containers), ("failed", {}))
fresh()
daemon.behaviour[("POST", "start")] = lambda path, body: (daemon.containers["c" * 64].update(status="running"),
                                                          (_ for _ in ()).throw(TimeoutError()))[1]
result = attempt()
check("start timed out but the worker runs: the job is accepted, not reported failed",
      (isinstance(result, dict) and result["status"], ps.status()["status"]), ("queued", "starting"))
fresh()
daemon.behaviour[("POST", "create")] = lambda path, body: (500, {"message": "no"})
result = attempt()
check("create refused and no container of the job: failed, nothing changed",
      (str(result).startswith("refused"), ps.status()["status"], ps.status()["error_code"]),
      (True, "failed", "dispatch_failed"))
fresh()


def create_then_timeout(path, body):
    daemon.containers["d" * 64] = {"status": "created", "labels": body["Labels"]}
    raise TimeoutError()


daemon.behaviour[("POST", "create")] = create_then_timeout
result = attempt()
check("create timed out but the daemon has the job's container: it is found by label and started",
      (isinstance(result, dict) and result["status"], daemon.containers["d" * 64]["status"]), ("queued", "running"))
fresh()
daemon.behaviour[("GET", "json")] = lambda path, body: (_ for _ in ()).throw(OSError())
jid_result = attempt()
check("Docker unreachable after dispatch: unknown, blocks a new update",
      (ps.status()["status"], str(attempt(OTHER)).startswith("refused")), ("unknown", True))

# ---------------------------------------------------------------- locks
fresh()
fd = held_cli_lock()
check("an update.sh running from SSH (its lock held): the queue refuses", str(attempt()).startswith("refused"), True)
os.close(fd)
jid = attempt()["job_id"]
with ps.queue_lock() as held:
    during = ps.status()
check("status while a request dispatches: reported, never decided", (held, during["status"]), (True, "queued"))

# ---------------------------------------------------------------- the API
fresh()
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
root = models.AdminUser(username="root", hashed_password=hash_password(PASSWORD), is_superadmin=True)
admin = models.AdminUser(username="sub", hashed_password=hash_password(PASSWORD), is_superadmin=False, role="admin")
db.add_all([root, admin])
db.commit()
current = {"admin": root}
app = FastAPI()
app.include_router(panel_settings.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_admin] = lambda: current["admin"]
client = TestClient(app)
queued = []
self_update.apply_update = lambda target, actor: (queued.append((target, actor)), {"status": "queued"})[1]
ok = {"X-Confirm-Password": PASSWORD}
check("API: no password: refused", client.post("/api/settings/update/apply", json={"target": TARGET, "confirm": True}).status_code, 403)
check("...wrong password: refused", client.post("/api/settings/update/apply", json={"target": TARGET, "confirm": True},
                                                headers={"X-Confirm-Password": "wrong"}).status_code, 403)
check("...no second confirmation: refused", client.post("/api/settings/update/apply", json={"target": TARGET},
                                                        headers=ok).status_code, 400)
check("...a short SHA: refused", client.post("/api/settings/update/apply", json={"target": "abc1234", "confirm": True},
                                             headers=ok).status_code, 422)
current["admin"] = admin
check("...a non-superadmin: refused", client.post("/api/settings/update/apply", json={"target": TARGET, "confirm": True},
                                                  headers=ok).status_code, 403)
check("...nothing queued by any refused request", queued, [])
current["admin"] = root
response = client.post("/api/settings/update/apply", json={"target": TARGET, "confirm": True}, headers=ok)
check("...superadmin, password, confirmation, full SHA: queued (202)", (response.status_code, queued), (202, [(TARGET, root.id)]))
check("...status endpoint answers for the superadmin", client.get("/api/settings/update/status").json()["status"], "idle")

# ---------------------------------------------------------------- no secret anywhere
fresh()
jid = attempt()["job_id"]
blob = b"".join(p.read_bytes() for p in (ps.ROOT / jid).rglob("*") if p.is_file())
check("no password or token in any job file or in the worker's command",
      (PASSWORD.encode() in blob, PASSWORD in json.dumps(daemon.created_bodies), "Env" in daemon.created_bodies[0]),
      (False, False, False))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
