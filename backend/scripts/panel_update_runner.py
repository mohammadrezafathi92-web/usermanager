"""Frozen sibling runner for the SAME update.sh used by the SSH/CLI path.

No application imports, JWTs, passwords, database startup or node calls.
Only known progress lines are exposed; build/git output is not persisted.

Every frozen input (this runner, update.sh, the helper, the compose binary
and the docker/flock shims) is checked against config.json's digests - which
the container's immutable Cmd anchors - and then COPIED into a directory of
this container's own filesystem and checked again there. update.sh runs only
from that private copy, so nothing in the shared job folder can change what
runs after the check.

The whole run has a deadline. At the deadline (or on SIGTERM) update.sh's
process group is stopped (TERM, then KILL) and the run is recorded as
INTERRUPTED, never as failed: a stop in the middle of a deployment leaves
an unknown state that an operator must look at before anything is retried.
"""
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlencode, quote

PHASES = {"preflight", "backup", "checkout", "build", "backend", "frontend"}
# Exactly these files make up a job; bin/ is first on update.sh's PATH, so
# nothing else may be in it.
FROZEN = ("runner.py", "update.sh", "helper.py", "compose", "bin/docker", "bin/flock")
SHIMS = ("docker", "flock")
STOP_GRACE = 20


class Interrupted(Exception):
    pass


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def state_file(path, value):
    temporary = str(path) + ".tmp"
    with open(temporary, "w", encoding="utf8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def verify(folder, hashes):
    """Every frozen file present, regular, unchanged; bin/ holds nothing else."""
    if set(hashes) != set(FROZEN):
        raise RuntimeError("frozen_input_changed")
    bin_dir = folder / "bin"
    if bin_dir.is_symlink() or not bin_dir.is_dir() or sorted(p.name for p in bin_dir.iterdir()) != sorted(SHIMS):
        raise RuntimeError("frozen_input_changed")
    for name, expected in hashes.items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise RuntimeError("frozen_input_changed")


def private_copy(folder, hashes):
    """The frozen files in this container's own filesystem, checked again."""
    private = Path(tempfile.mkdtemp(prefix="um-safe-update-"))
    try:
        (private / "bin").mkdir(mode=0o700)
        for name in FROZEN:
            shutil.copyfile(folder / name, private / name)
            (private / name).chmod(0o600 if name in ("update.sh", "helper.py") else 0o700)
        verify(private, hashes)
    except BaseException:
        shutil.rmtree(private, ignore_errors=True)
        raise
    return private


def docker_api(method, path):
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(10)
            self.sock.connect("/var/run/docker.sock")
    connection = UnixConnection("localhost", timeout=10)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        raw = response.read()
        if not 200 <= response.status < 300:
            raise RuntimeError("docker_request_failed")
        return json.loads(raw) if raw else {}
    finally:
        connection.close()


def docker_shim(args, folder):
    """Only commands used by frozen update.sh; never arbitrary shell input."""
    if args and args[0] == "compose":
        compose = str(folder / "compose")
        os.execv(compose, [compose, *args[1:]])
    if len(args) == 4 and args[:3] == ["inspect", "--format", "{{.Image}}"]:
        info = docker_api("GET", "/containers/" + quote(args[3], safe="") + "/json")
        image = info.get("Image", "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("image_identity_unavailable")
        print(image)
        return
    if len(args) == 4 and args[:2] == ["image", "tag"]:
        image, name = args[2:]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise RuntimeError("invalid_image_identity")
        if not re.fullmatch(r"usermanager-(backend|frontend):pre-update_[A-Za-z0-9_]+", name):
            raise RuntimeError("invalid_rollback_reference")
        repo, tag = name.rsplit(":", 1)
        docker_api("POST", "/images/" + quote(image, safe="") + "/tag?" + urlencode({"repo": repo, "tag": tag}))
        return
    raise RuntimeError("unsupported_docker_command")


def stop_group(process):
    """update.sh and everything it started (git, compose, the helper)."""
    for sig, wait in ((signal.SIGTERM, STOP_GRACE), (signal.SIGKILL, 10)):
        if process.poll() is not None:
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            continue
    # a grandchild left in the group still holds the pipe; it goes too
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run(config, folder):
    os.umask(0o077)
    value = {"job_id": config["job_id"], "target": config["target"],
             "status": "running", "phase": "preflight", "updated": False,
             "started_at": int(time.time()), "deadline_seconds": int(config["deadline_seconds"])}
    path = folder / "state.json"
    process = None
    private = None
    timer = None
    expired = threading.Event()

    def on_signal(_signum, _frame):
        raise Interrupted("stopped")
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    try:
        try:
            verify(folder, config["hashes"])
            private = private_copy(folder, config["hashes"])
        except Exception:
            # nothing ran: update.sh was never started
            value.update(status="failed", error_code="frozen_input_changed")
            raise
        state_file(path, value)
        environment = dict(os.environ, UM_UPDATE_PROJECT_DIR=config["project"],
            UM_UPDATE_HELPER_PATH=str(private / "helper.py"),
            UM_UPDATE_BACKUP_DIR=str(folder / "backup"),
            PATH=str(private / "bin") + os.pathsep + os.environ.get("PATH", ""))
        process = subprocess.Popen(["bash", str(private / "update.sh"), "--target", config["target"]],
            cwd=config["project"], env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)

        def deadline():
            expired.set()
            stop_group(process)
        timer = threading.Timer(value["deadline_seconds"], deadline)
        timer.daemon = True
        timer.start()
        succeeded = False
        # Do not expose raw git/build output: it can contain credentials.
        for chunk in iter(lambda: process.stdout.readline(8192), b""):
            line = chunk.decode("utf8", "replace").strip()
            if line.startswith("UPDATE_STAGE=") and line[13:] in PHASES:
                value["phase"] = line[13:]
                state_file(path, value)
            if line == f"UPDATE_OK commit={config['target']}; modes preserved; license-server untouched":
                succeeded = True
        code = process.wait(timeout=30)
        timer.cancel()
        if expired.is_set():
            raise Interrupted("deadline")
        if code != 0 or not succeeded:
            # update.sh stopped itself (its own STOP): a definite failure
            value.update(status="failed", error_code="safe_update_failed", exit_code=code)
        else:
            value.update(status="completed", phase="completed", updated=True, commit=config["target"])
    except BaseException as exc:
        if timer is not None:
            timer.cancel()
        if process is not None:
            stop_group(process)
            if value["status"] not in ("failed", "completed"):
                # stopped from outside while update.sh was running: unknown state
                value.update(status="interrupted",
                             error_code="deadline_exceeded" if expired.is_set() or str(exc) == "deadline"
                             else "runner_stopped")
        elif value["status"] != "failed":
            value.update(status="failed", error_code="safe_update_failed")
    if private is not None:
        shutil.rmtree(private, ignore_errors=True)
    value["finished_at"] = int(time.time())
    state_file(path, value)
    return 0 if value["status"] == "completed" else 1


def main():
    folder = Path(__file__).resolve().parent
    if len(sys.argv) > 1 and sys.argv[1] == "docker":
        docker_shim(sys.argv[2:], folder)
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "flock":
        if sys.argv[2:] != ["-n", "9"]:
            raise RuntimeError("unsupported_lock_command")
        # the same lock file as the host's flock(1): one update at a time
        fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return 0
    # the bytes the bootstrap verified against the container's Cmd (a second
    # read of config.json could see a different file)
    verified = globals().get("VERIFIED_CONFIG")
    if verified is None:
        raise RuntimeError("unverified_config")
    return run(json.loads(verified), folder)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # Even Docker daemon errors can include private paths/configuration.
        print("STOP: updater adapter failed", file=sys.stderr)
        raise SystemExit(1)
