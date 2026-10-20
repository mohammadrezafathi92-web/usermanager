"""The settings-page "بروزرسانی پنل" button must not take the panel down.

Run:  python3 backend/tests/test_self_update_restart.py

The live bug: self_update.apply_update()'s final step used to run
`docker compose up -d` as an ordinary subprocess of THIS process - which
lives inside usermanager-backend, the very container that command is about
to recreate. The daemon killing that container to recreate it kills the
subprocess driving the recreation too, mid-operation, leaving a container
that never finished coming down - so the panel goes dark and the NEXT
update attempt fails with "name already in use" (needed a manual
`docker rm -f` over SSH to clear, on the live server).

The first fix handed that step to a throwaway SIBLING container (see
local_deploy.spawn_sibling_container, still used elsewhere). The button now
goes further: apply_update() only queues the same safe update.sh the SSH
path runs, in a pinned worker (services/panel_safe_update.py). These tests
pin that the sibling container is built correctly, and that apply_update()
never pulls, resets, discards server changes, builds or restarts in-process.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import local_deploy, self_update

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


_orig_docker_api = local_deploy._docker_api
_orig_docker_request = local_deploy._docker_request


def restore_local_deploy():
    local_deploy._docker_api = _orig_docker_api
    local_deploy._docker_request = _orig_docker_request


print("--- spawn_sibling_container: the happy path ---")
calls = []


def fake_docker_api(path):
    calls.append(("GET", path))
    return {"Config": {"Image": "usermanager-backend:latest"}}


def fake_docker_request(method, path, body=None, timeout=15):
    calls.append((method, path, body))
    if method == "POST" and path == "/containers/create":
        return 201, {"Id": "abc123def456"}
    if method == "POST" and path == "/containers/abc123def456/start":
        return 204, {}
    return 500, {"message": "unexpected call in test"}


local_deploy._docker_api = fake_docker_api
local_deploy._docker_request = fake_docker_request

local_deploy.spawn_sibling_container(
    [f"{local_deploy.HOST_PROJECT_DIR}/backend/data/.docker-cli/docker-compose", "-f",
     f"{local_deploy.HOST_PROJECT_DIR}/docker-compose.yml", "up", "-d", "--remove-orphans"],
    log_path=f"{local_deploy.HOST_PROJECT_DIR}/backend/data/.self_update_restart.log",
)

check("inspects the backend container first",
      calls[0], ("GET", f"/containers/{local_deploy.BACKEND_CONTAINER_NAME}/json"))

create_call = next(c for c in calls if c[0] == "POST" and c[1] == "/containers/create")
body = create_call[2]
check("uses the SAME image already running - no pull needed", body["Image"], "usermanager-backend:latest")
check("no baked-in entrypoint - Cmd runs directly", body["Entrypoint"], [])
check("mounts the docker socket",
      f"{local_deploy.DOCKER_SOCK}:{local_deploy.DOCKER_SOCK}" in body["HostConfig"]["Binds"], True)
check("mounts the project dir at the SAME path both sides (like docker-compose.yml's own convention)",
      f"{local_deploy.HOST_PROJECT_DIR}:{local_deploy.HOST_PROJECT_DIR}" in body["HostConfig"]["Binds"], True)
check("cleans itself up once it's done", body["HostConfig"]["AutoRemove"], True)
check("output is redirected via a shell (image has /bin/sh)", body["Cmd"][:2], ["/bin/sh", "-c"])
check("...running the real compose command",
      "docker-compose" in body["Cmd"][2] and "up -d --remove-orphans" in body["Cmd"][2], True)
check("...with its output captured to the log file", body["Cmd"][2].rstrip().endswith("2>&1"), True)

start_call = next(c for c in calls if c[0] == "POST" and c[1].endswith("/start"))
check("then starts it", start_call[1], "/containers/abc123def456/start")

print("\n--- spawn_sibling_container: the container can't even be inspected ---")
local_deploy._docker_api = lambda path: None
try:
    local_deploy.spawn_sibling_container(["echo", "hi"])
    failures.append("should have raised when container info is unavailable")
    print("FAIL  raises DeployError")
except local_deploy.DeployError:
    print("PASS  raises DeployError instead of crashing or silently doing nothing")

print("\n--- spawn_sibling_container: the daemon refuses to create it ---")
local_deploy._docker_api = lambda path: {"Config": {"Image": "img"}}
local_deploy._docker_request = lambda method, path, body=None, timeout=15: (500, {"message": "daemon says no"})
try:
    local_deploy.spawn_sibling_container(["echo", "hi"])
    failures.append("should have raised when container create fails")
    print("FAIL  raises DeployError")
except local_deploy.DeployError as exc:
    check("...and the daemon's own message is preserved for the admin", "daemon says no" in exc.log, True)

restore_local_deploy()

print("\n--- spawn_sibling_container: the daemon creates it but refuses to start it ---")
local_deploy._docker_api = lambda path: {"Config": {"Image": "img"}}


def create_ok_start_fails(method, path, body=None, timeout=15):
    if path == "/containers/create":
        return 201, {"Id": "xyz"}
    return 500, {"message": "start refused"}


local_deploy._docker_request = create_ok_start_fails
try:
    local_deploy.spawn_sibling_container(["echo", "hi"])
    failures.append("should have raised when container start fails")
    print("FAIL  raises DeployError")
except local_deploy.DeployError as exc:
    check("...and says so", "start refused" in exc.log, True)

restore_local_deploy()

print("\n--- apply_update(): only queues the safe update.sh - nothing pulled, built, reset or restarted here ---")

TARGET = "a" * 40
queued = []
git_calls = []
state = {"dirty": False, "dirty_files": [], "branch": "main", "remote": TARGET}


def fake_git(*args, timeout=None):
    git_calls.append(args)
    if args[:2] == ("rev-parse", "--short"):
        return 0, "old1234"
    if args[:2] == ("rev-parse", "--abbrev-ref"):
        return 0, state["branch"]
    if args[0] == "status":
        return 0, "\n".join(f" M {f}" for f in state["dirty_files"])
    if args[:2] == ("rev-parse", "--verify"):
        return 0, state["remote"]
    return 0, ""


from app.services import panel_safe_update

_orig = {"_git": self_update._git, "_repo_is_git": self_update._repo_is_git,
         "verify_host_path": self_update.verify_host_path, "queue": panel_safe_update.queue}


def attempt():
    try:
        return self_update.apply_update(TARGET, 7)
    except local_deploy.DeployError as exc:
        return f"refused: {exc}"


try:
    self_update._git = fake_git
    self_update._repo_is_git = lambda: True
    self_update.verify_host_path = lambda: local_deploy.HOST_PROJECT_DIR
    panel_safe_update.queue = lambda target, actor: (queued.append((target, actor)),
                                                     {"job_id": "j" * 32, "status": "queued", "updated": False})[1]

    result = attempt()
    check("a clean main at the checked target: the job is queued, not reported as updated",
          (result.get("status"), result.get("updated")), ("queued", False))
    check("...with exactly that target and the acting admin", queued, [(TARGET, 7)])
    mutating = [c for c in git_calls if c[0] in ("pull", "reset", "checkout", "merge", "fetch")]
    check("...no pull, reset, checkout, merge or fetch in the API process", mutating, [])
    check("...no compose / sibling restart helpers left in self_update",
          [name for name in ("_compose", "spawn_sibling_container", "_restore_frontend_port") if hasattr(self_update, name)], [])

    queued.clear(); git_calls.clear()
    state.update(dirty_files=["docker-compose.yml"])
    result = attempt()
    check("a locally changed docker-compose.yml is refused, NOT checked out / discarded",
          (str(result).startswith("refused"), queued, [c for c in git_calls if c[0] == "checkout"]), (True, [], []))
    state.update(dirty_files=[])

    state.update(remote="b" * 40)
    check("a target that is not the checked main is refused", (str(attempt()).startswith("refused"), queued), (True, []))
    state.update(remote=TARGET, branch="feature")
    check("a checkout off main is refused", (str(attempt()).startswith("refused"), queued), (True, []))
    state.update(branch="main")
    try:
        self_update.apply_update("abc1234", 7)
        short = "accepted"
    except local_deploy.DeployError:
        short = "refused"
    check("a short SHA is refused", (short, queued), ("refused", []))
finally:
    self_update._git = _orig["_git"]
    self_update._repo_is_git = _orig["_repo_is_git"]
    self_update.verify_host_path = _orig["verify_host_path"]
    panel_safe_update.queue = _orig["queue"]

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
