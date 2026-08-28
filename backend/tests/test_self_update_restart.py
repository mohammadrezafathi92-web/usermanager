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

The fix hands that one step to a throwaway SIBLING container instead (see
local_deploy.spawn_sibling_container) - independent of the container being
replaced, so it survives. These tests pin two things: that the sibling
container is built correctly (right image, right mounts, right command),
and that apply_update() actually uses it instead of ever running
`docker compose up` in-process again.
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

print("\n--- apply_update(): the actual wiring - no more in-process 'up' ---")

compose_calls = []
sibling_calls = []

_git_responses = iter([
    (0, "old1234"), (0, "main"), (0, ""),   # current_revision() BEFORE the pull
    (0, "pulled ok"),                        # git pull
    (0, "new5678"), (0, "main"), (0, ""),   # current_revision() AFTER the pull
])

_orig = {
    "_git": self_update._git,
    "_repo_is_git": self_update._repo_is_git,
    "verify_host_path": self_update.verify_host_path,
    "_compose": self_update._compose,
    "spawn_sibling_container": self_update.spawn_sibling_container,
    "ensure_docker_compose_cli": self_update.ensure_docker_compose_cli,
    "Thread": self_update.threading.Thread,
    "sleep": self_update.time.sleep,
}


class SyncThread:
    """Runs the target immediately instead of on a real thread, so the test
    doesn't have to sleep/poll to observe what the background restart step
    did."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


try:
    self_update._git = lambda *a, timeout=None: next(_git_responses, (0, ""))
    self_update._repo_is_git = lambda: True
    self_update.verify_host_path = lambda: local_deploy.HOST_PROJECT_DIR
    self_update._compose = lambda *args, timeout=None: (compose_calls.append(args), (0, "build ok"))[1]
    self_update.spawn_sibling_container = lambda cmd, log_path=None: sibling_calls.append((cmd, log_path))
    self_update.ensure_docker_compose_cli = lambda: "/app/data/.docker-cli/docker-compose"
    self_update.threading.Thread = SyncThread
    self_update.time.sleep = lambda *_: None

    result = self_update.apply_update()

    check("reports the update as applied", result.get("updated"), True)
    check("the build still ran in-process (safe - old container is still up)",
          any(c and c[0] == "build" for c in compose_calls), True)
    check("but 'up' NEVER ran in-process again - that was the whole bug",
          any(c and c[0] == "up" for c in compose_calls), False)
    check("the recreate ran through the sibling container instead", len(sibling_calls), 1)

    cmd, log_path = sibling_calls[0]
    check("...addressing the compose binary via HOST_PROJECT_DIR, not /app/data "
          "(the sibling container has no /app/data mount)",
          cmd[0].startswith(local_deploy.HOST_PROJECT_DIR), True)
    check("...running 'up -d'", "up" in cmd and "-d" in cmd, True)
    check("...with a log file so a stuck restart is still debuggable", bool(log_path), True)
finally:
    self_update._git = _orig["_git"]
    self_update._repo_is_git = _orig["_repo_is_git"]
    self_update.verify_host_path = _orig["verify_host_path"]
    self_update._compose = _orig["_compose"]
    self_update.spawn_sibling_container = _orig["spawn_sibling_container"]
    self_update.ensure_docker_compose_cli = _orig["ensure_docker_compose_cli"]
    self_update.threading.Thread = _orig["Thread"]
    self_update.time.sleep = _orig["sleep"]

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
