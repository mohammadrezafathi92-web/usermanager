"""The panel's update worker (no docker CLI) answers update.sh's base-image
check and pull through the Docker API - and reports a failed pull, which
the daemon hides inside an HTTP 200 stream, as a failure."""
import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("panel_update_runner", ROOT / "backend/scripts/panel_update_runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
checks = 0


def check(label, condition):
    global checks
    assert condition, label
    checks += 1
    print("PASS", label)


check("tag kept exactly", runner.pull_params("python:3.11-slim") == {"fromImage": "python", "tag": "3.11-slim"})
check("registry port is not a tag", runner.pull_params("registry.local:5000/team/app") ==
      {"fromImage": "registry.local:5000/team/app", "tag": "latest"})
check("a digest pin is pulled as written", runner.pull_params("nginx@sha256:" + "a" * 64) ==
      {"fromImage": "nginx@sha256:" + "a" * 64})
check("an error line in a 200 stream is a failure",
      runner.pull_failed(b'{"status":"Pulling"}\n{"error":"unexpected status from HEAD request"}\n'))
check("a clean stream is a success", not runner.pull_failed(b'{"status":"Pulling"}\n{"status":"Downloaded newer image"}\n'))

seen = []
with patch.object(runner, "docker_api", lambda method, path: seen.append((method, path)) or {}):
    runner.docker_shim(["image", "inspect", "python:3.11-slim"], None)
check("inspect asks the daemon for that exact image", seen == [("GET", "/images/python%3A3.11-slim/json")])


def missing(method, path):
    raise RuntimeError("docker_request_failed")


with patch.object(runner, "docker_api", missing):
    try:
        runner.docker_shim(["image", "inspect", "python:3.11-slim"], None)
        code = 0
    except SystemExit as exc:
        code = exc.code
check("a missing image is exit status 1 (update.sh then pulls)", code == 1)

pulled = []
with patch.object(runner, "docker_pull", pulled.append):
    runner.docker_shim(["pull", "--quiet", "node:20-alpine"], None)
check("pull goes to the API with the reference as written", pulled == ["node:20-alpine"])

for bad in (["pull", "--quiet", "Bad Ref;rm"], ["image", "inspect", "$(x)"], ["pull", "node:20"], ["rmi", "x"]):
    try:
        runner.docker_shim(bad, None)
        refused = False
    except RuntimeError:
        refused = True
    check(f"refused: {' '.join(bad)}", refused)

print(f"\n{checks} checks passed")
