"""Authenticated UI queue; execution belongs to a pinned sibling container.

Jobs contain no session token/password. State survives backend recreation.
The worker runs a frozen copy of update.sh, not a second deployment protocol.

Who writes a job's state.json: the backend only while no runner can be
running (before the container exists, after it is gone, or after a container
that never started has been removed); otherwise only the runner. A state
that cannot be decided (the runner gone mid-deployment, Docker unreachable,
an ambiguous dispatch) is reported as "unknown" and blocks every new update
until an operator resolves it (resolve()).
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import time
import uuid
from contextlib import contextmanager
from urllib.parse import quote

from . import local_deploy
from .local_deploy import DeployError

DATA = Path(os.environ.get("APP_DATA_DIR", "/app/data"))
ROOT = DATA / "update-runs"
# the whole worker run, update.sh included (the runner stops it there)
DEADLINE_SECONDS = 3600
TERMINAL = {"completed", "failed", "abandoned"}
ACTIVE = {"queued", "starting", "running"}
STAGES = {"queued", "preflight", "backup", "checkout", "build", "backend", "frontend", "completed"}
FROZEN = ("runner.py", "update.sh", "helper.py", "compose", "bin/docker", "bin/flock")
LABEL = "usermanager.safe-update"
HOST_ID = Path("/etc/usermanager/host.id")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with open(temporary, "w", encoding="utf8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def shim_text(name, python):
    """A location-independent shim: it runs the runner next to its own bin/,
    so the runner's private copy calls the private runner. The interpreter is
    the pinned image's own, quoted (never an arbitrary caller command)."""
    return ("#!/bin/sh\n"
            'here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd) || exit 1\n'
            f'exec {shlex.quote(python)} "$here/runner.py" {name} "$@"\n')


@contextmanager
def queue_lock(wait=False):
    """Yields True with the queue lock held, False when another request holds
    it (only with wait=False... and then the caller must not decide)."""
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(ROOT / "queue.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        os.close(fd)


def folder_for(job_id):
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise DeployError("شناسهٔ آپدیت نامعتبر است")
    folder = ROOT / job_id
    if folder.is_symlink():
        raise DeployError("مسیر آپدیت معتبر نیست")
    return folder


def _read(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        raise DeployError("وضعیت آپدیت نامعلوم است؛ پیش از تکرار بررسی شود") from None


def _inspect(container_id):
    """running | created | exited | gone | unknown - never a guess."""
    try:
        code, info = local_deploy._docker_request("GET", f"/containers/{quote(container_id, safe='')}/json")
    except Exception:
        return "unknown"
    if code == 404:
        return "gone"
    if code != 200:
        return "unknown"
    state = (info.get("State") or {}).get("Status")
    if state in ("running", "restarting", "paused"):
        return "running"
    if state == "created":
        return "created"
    if state in ("exited", "dead", "removing"):
        return "exited"
    return "unknown"


def _labelled(job_id):
    """Containers of this job, from the daemon (None: it could not say)."""
    filters = quote(json.dumps({"label": [f"{LABEL}={job_id}"]}))
    try:
        code, body = local_deploy._docker_request("GET", f"/containers/json?all=1&filters={filters}")
    except Exception:
        return None
    if code != 200 or not isinstance(body, list):
        return None
    return [c.get("Id") for c in body if c.get("Id")]


def cli_lock_free():
    """update.sh's own lock (the CLI and this queue share it): True free,
    False held, None cannot tell."""
    git_dir = Path(local_deploy.HOST_PROJECT_DIR) / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        return None
    try:
        fd = os.open(git_dir / "usermanager-update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return None
    finally:
        os.close(fd)


def _active_id():
    active = _read(ROOT / "active.json")
    if active is None:
        return None
    if not isinstance(active, dict) or "job_id" not in active:
        raise DeployError("وضعیت آپدیت قابل خواندن نیست؛ درخواست تازه نفرستید")
    return active["job_id"]


def _container_id(folder, job_id):
    """(id or None, decided): container.json first, then the daemon's labels."""
    recorded = _read(folder / "container.json")
    if recorded and recorded.get("container_id"):
        return recorded["container_id"], True
    ids = _labelled(job_id)
    if ids is None:
        return None, False
    return (ids[0] if ids else None), True


def _status(job_id):
    folder = folder_for(job_id)
    config, raw = _read(folder / "config.json"), _read(folder / "state.json")
    if config is None or raw is None:
        raise DeployError("وضعیت آپدیت نامعلوم است؛ پیش از تکرار بررسی شود")
    state = raw.get("status")
    result = {"job_id": job_id, "target": config["target"], "updated": False, "resolvable": False,
              "phase": raw.get("phase") if raw.get("phase") in STAGES else "queued",
              "started_at": raw.get("started_at"), "finished_at": raw.get("finished_at")}
    if state == "completed":
        if raw.get("commit") != config["target"] or raw.get("updated") is not True:
            raise DeployError("نتیجهٔ آپدیت ناسازگار است؛ پیش از تکرار بررسی شود")
        return {**result, "status": "completed", "updated": True, "commit": config["target"]}
    if state in ("failed", "abandoned"):
        return {**result, "status": state, "error_code": raw.get("error_code", "safe_update_failed")}
    if state == "interrupted":
        # stopped while update.sh ran: what it changed is unknown
        return {**result, "status": "unknown", "error_code": raw.get("error_code", "interrupted"),
                "resolvable": True}
    if state not in ("queued", "running"):
        return {**result, "status": "unknown", "error_code": "state_unreadable"}
    container_id, decided = _container_id(folder, job_id)
    if not decided:
        return {**result, "status": "unknown", "error_code": "docker_unreachable"}
    where = "gone" if container_id is None else _inspect(container_id)
    if where == "unknown":
        return {**result, "status": "unknown", "error_code": "docker_unreachable"}
    if where == "created":
        # created, never started (the dispatch is over: no lock here)
        return {**result, "status": "starting", "error_code": "not_started", "resolvable": True}
    if where == "running":
        overdue = bool(raw.get("started_at")) and time.time() > raw["started_at"] + DEADLINE_SECONDS + 600
        return {**result, "status": "running" if state == "running" else "starting", "overdue": overdue}
    # the runner is gone without a final state
    if state == "queued":
        # it never wrote "running": update.sh was never started
        return {**result, "status": "failed", "error_code": "not_started"}
    return {**result, "status": "unknown", "error_code": "runner_lost", "resolvable": True}


def status(job_id=None):
    if job_id is None:
        job_id = _active_id()
        if job_id is None:
            return {"status": "idle", "updated": False}
    with queue_lock() as held:
        if not held:
            # a request is dispatching right now: report, never decide
            raw = _read(folder_for(job_id) / "state.json") or {}
            return {"job_id": job_id, "status": "queued", "phase": "queued", "updated": False, "resolvable": False,
                    "dispatching": raw.get("status") == "queued"}
        return _status(job_id)


def mounts(info):
    project = local_deploy.HOST_PROJECT_DIR
    found = {m.get("Destination"): m for m in info.get("Mounts", []) if m.get("Type") == "bind"}
    needed = (project, str(DATA), "/etc/usermanager")
    if any(destination not in found for destination in needed):
        raise DeployError("مسیر پروژه، داده یا هویت میزبان تأیید نشد؛ از آپدیت امن SSH استفاده کنید")
    if found[project].get("Source") != project or found[project].get("RW") is not True:
        raise DeployError("مسیر واقعی پروژه با مسیر کانتینر یکسان نیست")
    if found[str(DATA)].get("RW") is not True or found["/etc/usermanager"].get("RW") is not False:
        raise DeployError("دسترسی mount داده یا هویت میزبان معتبر نیست")
    binds = [f"{local_deploy.DOCKER_SOCK}:{local_deploy.DOCKER_SOCK}"]
    for destination in needed:
        source = found[destination].get("Source", "")
        if not source.startswith("/") or ":" in source or ":" in destination:
            raise DeployError("مسیر mount معتبر نیست")
        binds.append(f"{source}:{destination}" + (":ro" if destination == "/etc/usermanager" else ""))
    return binds


def _prepare(job_id, target, actor_id, binary):
    folder = folder_for(job_id)
    folder.mkdir(mode=0o700)
    (folder / "bin").mkdir(mode=0o700)
    project = Path(local_deploy.HOST_PROJECT_DIR)
    shipped = Path(__file__).resolve().parents[2] / "scripts"
    # In Docker /app/scripts is immutable image content. Source installs
    # reach backend/scripts at the same relative location.
    sources = {"runner.py": shipped / "panel_update_runner.py", "helper.py": shipped / "update_preflight.py",
               "update.sh": project / "update.sh", "compose": binary}
    for name, source in sources.items():
        if source.is_symlink() or not source.is_file():
            raise DeployError("یکی از فایل‌های آپدیت قابل تأیید نیست")
        shutil.copyfile(source, folder / name)
        (folder / name).chmod(0o700 if name == "compose" else 0o600)
    for name in ("docker", "flock"):
        (folder / "bin" / name).write_text(shim_text(name, sys.executable))
        (folder / "bin" / name).chmod(0o700)
    config = {"job_id": job_id, "target": target, "actor_id": actor_id, "project": str(project),
              "deadline_seconds": DEADLINE_SECONDS, "created_at": int(time.time()),
              "hashes": {name: digest(folder / name) for name in FROZEN}}
    save(folder / "config.json", config)
    save(folder / "state.json", {"status": "queued", "phase": "queued", "updated": False})
    return folder, project


def _not_started(folder, error_code, **extra):
    save(folder / "state.json", {"status": "failed", "phase": "queued", "updated": False,
                                 "error_code": error_code, "finished_at": int(time.time()), **extra})


def queue(target, actor_id):
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40}", target):
        raise DeployError("ابتدا نسخهٔ جدید را بررسی کنید و صفحه را تازه کنید")
    from . import self_update
    with queue_lock() as held:
        if not held:
            raise DeployError("درخواست آپدیت دیگری در حال ثبت است؛ دوباره وضعیت را بررسی کنید")
        active = _active_id()
        existing = _status(active) if active else {"status": "idle"}
        if existing["status"] not in TERMINAL | {"idle"}:
            if existing.get("target") == target and existing["status"] in ACTIVE:
                return existing                         # the same request again (double click, retry)
            raise DeployError("آپدیت قبلی هنوز نتیجهٔ قطعی ندارد؛ درخواست تازه نفرستید")
        lock = cli_lock_free()
        if lock is not True:
            raise DeployError("آپدیت دیگری از SSH در حال اجراست یا قفل آن قابل بررسی نیست")
        info = self_update.current_revision()
        if info.get("dirty") or info.get("branch") != "main":
            raise DeployError("آپدیت فقط روی main بدون تغییر فایل‌های tracked مجاز است")
        # Probe the daemon BEFORE making an executable job; never guess paths
        # or use Config.Image (a mutable tag that build may replace).
        container = local_deploy._docker_api(f"/containers/{local_deploy.BACKEND_CONTAINER_NAME}/json")
        if not container or not re.fullmatch(r"sha256:[0-9a-f]{64}", container.get("Image", "")):
            raise DeployError("شناسهٔ ثابت image فعلی قابل تأیید نیست")
        binds = mounts(container)
        if not HOST_ID.is_file():
            raise DeployError("هویت میزبان موجود نیست؛ ابتدا آپدیت امن SSH را اجرا کنید")
        binary = Path(local_deploy.ensure_docker_compose_cli())
        if not binary.is_file() or binary.is_symlink() or not os.access(binary, os.X_OK):
            raise DeployError("ابزار آپدیت آماده نیست؛ از مسیر امن SSH استفاده کنید")
        job_id = uuid.uuid4().hex
        folder, project = _prepare(job_id, target, actor_id, binary)
        save(ROOT / "active.json", {"job_id": job_id})
        # Anchor BOTH runner and config hashes in Docker's immutable Cmd, and
        # run exactly the bytes that were hashed (no second read of either
        # file; no assert, which python -O would drop).
        bootstrap = ("import hashlib,pathlib,sys; "
                     "p=pathlib.Path(sys.argv[1]); r=p.read_bytes(); c=(p.parent/'config.json').read_bytes(); "
                     "ok=hashlib.sha256(r).hexdigest()==sys.argv[2] and hashlib.sha256(c).hexdigest()==sys.argv[3]; "
                     "ok or sys.exit('STOP: frozen input changed'); "
                     "sys.argv=[str(p)]; "
                     "exec(compile(r,str(p),'exec'),{'__name__':'__main__','__file__':str(p),'VERIFIED_CONFIG':c})")
        body = {"Image": container["Image"], "Entrypoint": [], "WorkingDir": str(project),
                "Cmd": [sys.executable, "-c", bootstrap, str(folder / "runner.py"),
                        digest(folder / "runner.py"), digest(folder / "config.json")],
                "HostConfig": {"Binds": binds, "AutoRemove": True},
                "Labels": {LABEL: job_id}}
        job = {"job_id": job_id, "target": target, "status": "queued", "phase": "queued", "updated": False}
        try:
            code, response = local_deploy._docker_request(
                "POST", f"/containers/create?name=usermanager-safe-update-{job_id[:12]}", body)
        except Exception:
            code, response = None, {}
        container_id = (response or {}).get("Id", "") if code == 201 else ""
        if not re.fullmatch(r"[0-9a-f]{12,64}", container_id or ""):
            found = _labelled(job_id)
            if found == [] and code is not None:
                # the daemon answered and no container of this job exists
                _not_started(folder, "dispatch_failed")
                raise DeployError("اجرای آپدیت شروع نشد؛ هیچ سرویس یا حالتی تغییر نکرد")
            if not found:
                raise DeployError("نتیجهٔ شروع آپدیت نامعلوم است؛ پیش از تکرار وضعیت را بررسی کنید")
            container_id = found[0]
        # Keep runtime identity separate: config stays immutable for the
        # bootstrap digest. The status reader uses this second file.
        save(folder / "container.json", {"container_id": container_id})
        try:
            code, _ = local_deploy._docker_request("POST", f"/containers/{container_id}/start")
        except Exception:
            code = None                                   # it may have started anyway
        if code in (204, 304):
            return job
        where = _inspect(container_id)
        if where in ("running", "exited", "gone"):
            return job                                    # it did start; state.json tells the rest
        if where == "created" and _remove_unstarted(container_id):
            _not_started(folder, "dispatch_failed")
            raise DeployError("اجرای آپدیت شروع نشد؛ هیچ سرویس یا حالتی تغییر نکرد")
        raise DeployError("نتیجهٔ شروع آپدیت نامعلوم است؛ پیش از تکرار وضعیت را بررسی کنید")


def _remove_unstarted(container_id):
    """Removes a container of ours that never started (without force: a
    container that is running by now is refused by the daemon and kept)."""
    try:
        code, _ = local_deploy._docker_request("DELETE", f"/containers/{quote(container_id, safe='')}")
    except Exception:
        return False
    return code in (204, 404)


def resolve(job_id, actor_id):
    """The operator closes an undecided job after checking the server:
      - a worker container created but never started is removed (nothing ran);
      - a runner that stopped mid-deployment is recorded as abandoned, only
        while no update.sh holds the CLI lock.
    Nothing is rolled back or restored; no new update is started."""
    with queue_lock() as held:
        if not held:
            raise DeployError("درخواست آپدیت دیگری در حال ثبت است؛ دوباره وضعیت را بررسی کنید")
        if _active_id() != job_id:
            raise DeployError("این آپدیت، آپدیت فعال نیست")
        current = _status(job_id)
        if not current.get("resolvable"):
            raise DeployError("این آپدیت اکنون قابل بستن نیست؛ وضعیت را دوباره بررسی کنید")
        folder = folder_for(job_id)
        stamp = {"resolved_by": actor_id, "resolved_at": int(time.time())}
        if current["status"] == "starting":
            container_id, decided = _container_id(folder, job_id)
            if not decided or (container_id and not _remove_unstarted(container_id)):
                raise DeployError("کانتینر آپدیت حذف نشد؛ وضعیت را دوباره بررسی کنید")
            _not_started(folder, "not_started", **stamp)
            return _status(job_id)
        if cli_lock_free() is not True:
            raise DeployError("update.sh هنوز قفل آپدیت را نگه داشته یا قفل قابل بررسی نیست")
        raw = _read(folder / "state.json") or {}
        save(folder / "state.json", {**raw, "status": "abandoned", "error_code": current.get("error_code"),
                                     "finished_at": raw.get("finished_at") or int(time.time()), **stamp})
        return _status(job_id)
