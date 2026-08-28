"""Automates changing THIS server's own panel web port (تنظیمات > پورت پنل
وب) with no SSH/host/password prompts at all - unlike remote_deploy.py
(which genuinely needs SSH because it targets a *different* server), this
feature only ever needs to modify a docker-compose.yml sitting right next
to this very container and restart one sibling container. Both things are
possible without SSH because docker-compose.yml mounts two things into the
backend container:

  - /var/run/docker.sock -> /var/run/docker.sock   (talk to the HOST's own
    docker daemon - the "docker outside of docker" pattern used by tools
    like Portainer/Watchtower/Traefik)
  - the project directory itself, bind-mounted at the SAME absolute path
    inside the container as it has on the host (HOST_PROJECT_DIR, default
    /root/usermanager) - this matters because when the docker-compose CLI
    submits a container-create request over the socket, the DAEMON resolves
    any relative bind-mount paths in docker-compose.yml (like
    "./backend/data:/app/data") against the path passed via `-f`, and the
    daemon looks for that path on the REAL host filesystem - not through
    this container's mount namespace. So the path only works if it's the
    actual host path, which is exactly why the mount destination and
    HOST_PROJECT_DIR must agree.

If the admin's install lives somewhere other than /root/usermanager, they
can override it via the HOST_PROJECT_DIR env var in backend/.env - but the
docker-compose.yml bind-mount destination has to be edited to match too, so
in practice this stays a fixed convention for a normal install.

The docker-compose static binary itself is NOT baked into the backend
image (it used to be - see git history of backend/Dockerfile - but that
cost ~370s and ~100MB+ on EVERY single image build for a feature almost
nobody uses on any given day). Instead ensure_docker_compose_cli() below
downloads it once, lazily, the first time this feature actually runs, and
caches it under the persistent ./backend/data bind mount so it survives
image rebuilds and is never re-downloaded after that. The v2 compose
binary works standalone (it doesn't need the separate `docker` CLI - it
talks to the daemon directly over DOCKER_HOST / the mounted socket), so we
only ever need this one binary, not two."""
from __future__ import annotations

import logging
import os
import platform
import stat
import subprocess

import requests

logger = logging.getLogger("local_deploy")

HOST_PROJECT_DIR = os.environ.get("HOST_PROJECT_DIR", "/root/usermanager")

DOCKER_CLI_VERSION = "25.0.3"
DOCKER_COMPOSE_VERSION = "2.24.6"
_CLI_CACHE_DIR = os.path.join(os.environ.get("APP_DATA_DIR", "/app/data"), ".docker-cli")
_COMPOSE_BIN = os.path.join(_CLI_CACHE_DIR, "docker-compose")


class DeployError(Exception):
    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except subprocess.TimeoutExpired as exc:
        return 124, "", str(exc)


def _compose_arch() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x86_64"
    if machine in ("aarch64", "arm64"):
        return "aarch64"
    raise DeployError(f"معماری پشتیبانی‌نشده برای دانلود docker-compose: {machine}")


def ensure_docker_compose_cli() -> str:
    """Downloads the docker-compose v2 static binary into the persistent
    cache on first use only. Returns the binary path to invoke."""
    if os.path.isfile(_COMPOSE_BIN) and os.access(_COMPOSE_BIN, os.X_OK):
        return _COMPOSE_BIN

    os.makedirs(_CLI_CACHE_DIR, exist_ok=True)
    arch = _compose_arch()
    url = (
        f"https://github.com/docker/compose/releases/download/"
        f"v{DOCKER_COMPOSE_VERSION}/docker-compose-linux-{arch}"
    )
    try:
        resp = requests.get(url, timeout=60)
        resp.raise_for_status()
        tmp_path = _COMPOSE_BIN + ".tmp"
        with open(tmp_path, "wb") as f:
            f.write(resp.content)
        os.chmod(tmp_path, os.stat(tmp_path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        os.replace(tmp_path, _COMPOSE_BIN)
    except Exception as exc:  # noqa: BLE001 - surfaced as a DeployError below
        raise DeployError(f"دانلود ابزار docker-compose ناموفق بود: {exc}") from exc

    return _COMPOSE_BIN


# --------------------------------------------------- host vs container paths
# The single most dangerous assumption in this file. Everything below that
# hands a path to the docker DAEMON is handing it a path on the HOST, while
# every path this process opens is a path inside THIS CONTAINER. The project
# is bind-mounted at `.:/root/usermanager`, so the container path is always
# /root/usermanager - but the host path is wherever the admin installed it,
# and install.sh picks /opt/usermanager on a fresh box.
#
# When the two differ, `docker compose up -d` issued from in here tells the
# daemon to bind-mount a host directory that does not exist. Docker happily
# creates it, empty, and recreates the containers around it - the panel comes
# back up with no database, no .env and no code. That is not theoretical: it
# happened on 2026-08-18 the moment the self-update button was first used on
# an /opt install.
#
# So the mismatch is DETECTED and the operation REFUSED. Rewriting paths on
# the fly was considered and rejected: `build` must read contexts and
# env_files from the container filesystem while `up` must resolve binds
# against the host, and no single --project-directory satisfies both. A
# guard that stops is worth more than a clever fix that might take the panel
# down a second time.

BACKEND_CONTAINER_NAME = "usermanager-backend"
DOCKER_SOCK = "/var/run/docker.sock"


def _docker_api(path: str) -> dict | None:
    """One GET against the docker daemon over its unix socket.

    Hand-rolled instead of pulling in the docker SDK: this is the only API
    call the project makes, and http.client speaks the protocol fine once
    given the right socket.
    """
    import http.client
    import json
    import socket as _socket

    class _UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self.sock.settimeout(5)
            self.sock.connect(DOCKER_SOCK)

    try:
        conn = _UnixConnection("localhost", timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        return json.loads(resp.read())
    except Exception:
        logger.warning("پرسش از داکر ناموفق بود: %s", path, exc_info=True)
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _docker_request(method: str, path: str, body: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    """Same unix-socket plumbing as _docker_api, generalised to POST (used
    to create/start a container) and to return the status code, which a
    caller needs to tell "created" from "already exists" from "daemon
    rejected this"."""
    import http.client
    import json
    import socket as _socket

    class _UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            self.sock.settimeout(timeout)
            self.sock.connect(DOCKER_SOCK)

    conn = _UnixConnection("localhost", timeout=timeout)
    try:
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {"message": raw.decode("utf-8", "replace")}
        return resp.status, parsed
    finally:
        try:
            conn.close()
        except Exception:
            pass


def spawn_sibling_container(cmd: list[str], *, log_path: str | None = None) -> None:
    """Runs `cmd` in a brand-new, independent container - NOT the one this
    process is running in - that shares this container's project bind-mount
    and docker socket, then removes itself when done.

    This exists for exactly one caller: services/self_update.py's final
    step, which must run `docker compose up -d` to recreate THIS very
    container (usermanager-backend). Running that as an ordinary subprocess
    of this process does not work: the subprocess lives in this container's
    own cgroup, so the moment the daemon stops/removes this container to
    recreate it, the subprocess driving that recreation is killed too, mid-
    operation. What's left behind is a container that never finished being
    torn down - and the NEXT update attempt then fails with "Error when
    allocating new name: ... already in use", exactly what happened on the
    live server on 2026-08-2x and needed a manual `docker rm -f` over SSH to
    clear.

    A sibling container has no such problem: it is not part of the compose
    project being recreated, so killing/replacing usermanager-backend does
    not touch it, and it finishes the job started by a process that, by the
    time it finishes, may already be gone.

    Uses the SAME image this container is currently running (already local,
    no pull needed - it was just built by the step before this one) and the
    same two bind mounts docker-compose.yml already gives this container
    (the docker socket, and the project directory at the same path on both
    sides), so `cmd` sees exactly what a command run in here would.
    """
    info = _docker_api(f"/containers/{BACKEND_CONTAINER_NAME}/json")
    if not info:
        raise DeployError("اطلاعات کانتینر پنل از داکر قابل خواندن نیست - کانتینر کمکی بروزرسانی ساخته نشد", "")
    image = (info.get("Config") or {}).get("Image")
    if not image:
        raise DeployError("نام ایمیج کانتینر پنل از داکر پیدا نشد", "")

    run_cmd = list(cmd)
    if log_path:
        # No shell in the mounted binary itself, so redirection needs one -
        # /bin/sh exists in this image (python:3.11-slim is Debian-based).
        quoted = " ".join(_shell_quote(part) for part in run_cmd)
        run_cmd = ["/bin/sh", "-c", f"{quoted} > {_shell_quote(log_path)} 2>&1"]

    create_body = {
        "Image": image,
        "Cmd": run_cmd,
        "Entrypoint": [],
        "WorkingDir": HOST_PROJECT_DIR,
        "HostConfig": {
            "Binds": [
                f"{DOCKER_SOCK}:{DOCKER_SOCK}",
                f"{HOST_PROJECT_DIR}:{HOST_PROJECT_DIR}",
            ],
            "AutoRemove": True,
        },
    }
    status, resp = _docker_request("POST", "/containers/create", create_body)
    if status not in (200, 201):
        raise DeployError("ساخت کانتینر کمکی بروزرسانی ناموفق بود", str(resp))
    container_id = resp.get("Id")
    if not container_id:
        raise DeployError("شناسه‌ی کانتینر کمکی بروزرسانی برگردانده نشد", str(resp))

    status, resp = _docker_request("POST", f"/containers/{container_id}/start")
    if status not in (200, 204):
        raise DeployError("اجرای کانتینر کمکی بروزرسانی ناموفق بود", str(resp))
    logger.info("بروزرسانی: کانتینر کمکی %s برای بازسازی نهایی شروع شد", container_id[:12])


def _shell_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


def detect_host_project_dir() -> str | None:
    """Where this container's project mount actually lives on the host.

    Asked of the daemon rather than guessed, because the daemon is the only
    party that knows both sides of a bind mount. Returns None when the answer
    cannot be established - callers treat that as "unknown", not as "fine".
    """
    info = _docker_api(f"/containers/{BACKEND_CONTAINER_NAME}/json")
    if not info:
        return None
    for mount in info.get("Mounts") or []:
        if mount.get("Destination") == HOST_PROJECT_DIR and mount.get("Source"):
            return mount["Source"]
    return None


def verify_host_path() -> str:
    """Raises DeployError unless the daemon-visible host path matches the
    path this container will hand it. Returns the confirmed host path."""
    actual = detect_host_project_dir()
    if actual is None:
        raise DeployError(
            "مسیر واقعی پروژه روی سرور قابل تشخیص نیست، و اجرای این عملیات بدون آن "
            "می‌تواند کانتینرها را با پوشه‌ی خالی بالا بیاورد. لطفا این کار را با SSH انجام دهید.",
            "",
        )
    if actual != HOST_PROJECT_DIR:
        raise DeployError(
            f"پروژه روی سرور در «{actual}» نصب شده ولی این کانتینر با مسیر «{HOST_PROJECT_DIR}» کار می‌کند. "
            "اگر از داخل پنل ادامه دهیم، داکر یک پوشه‌ی خالی می‌سازد و پنل بدون دیتابیس بالا می‌آید. "
            f"این کار را با SSH انجام دهید:\ncd {actual} && git pull && docker compose up -d --build",
            "",
        )
    return actual



def change_panel_port_local(current_port: int, new_port: int) -> str:
    """Rewrites the frontend service's host-side port mapping in
    docker-compose.yml (only the exact "CURRENT:80" string, same
    conservative approach the old SSH version used - never a generic port
    regex) and recreates just that one container via the local docker
    socket. Raises DeployError with a Persian message on any failure."""
    verify_host_path()
    compose_path = os.path.join(HOST_PROJECT_DIR, "docker-compose.yml")
    log_lines: list[str] = []

    def log(line: str) -> None:
        log_lines.append(line)
        logger.info(line)

    if not os.path.isfile(compose_path):
        raise DeployError(
            f"فایل docker-compose.yml در مسیر {compose_path} پیدا نشد. "
            f"احتمالا مسیر پروژه روی سرور با {HOST_PROJECT_DIR} فرق دارد.",
            "\n".join(log_lines),
        )
    log(f"فایل {compose_path} پیدا شد.")

    try:
        with open(compose_path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as exc:
        raise DeployError(f"خواندن docker-compose.yml ناموفق بود: {exc}", "\n".join(log_lines))

    old_mapping = f'"{current_port}:80"'
    new_mapping = f'"{new_port}:80"'
    if old_mapping not in content:
        raise DeployError(
            f"رشته {old_mapping} در docker-compose.yml پیدا نشد - احتمالا پورت فعلی با مقدار "
            f"ذخیره‌شده در پنل ({current_port}) یکی نیست. لطفا فایل را روی سرور دستی چک کنید.",
            "\n".join(log_lines),
        )
    log(f"در حال تغییر {old_mapping} به {new_mapping} ...")

    new_content = content.replace(old_mapping, new_mapping)
    try:
        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as exc:
        raise DeployError(f"نوشتن docker-compose.yml ناموفق بود: {exc}", "\n".join(log_lines))
    log("فایل docker-compose.yml بروزرسانی شد.")

    try:
        compose_bin = ensure_docker_compose_cli()
    except DeployError:
        with open(compose_path, "w", encoding="utf-8") as f:
            f.write(content)
        log("(فایل docker-compose.yml به حالت قبل بازگردانده شد.)")
        raise
    log("در حال بازسازی کانتینر frontend ...")
    code, out, err = _run([compose_bin, "-f", compose_path, "up", "-d", "frontend"], timeout=120)
    if code != 0:
        # Roll the file back to the old mapping so a failed attempt doesn't
        # leave docker-compose.yml pointing at a port that isn't actually
        # live - the next attempt (or next `docker compose up -d` the admin
        # runs by hand) shouldn't silently diverge from reality.
        try:
            with open(compose_path, "w", encoding="utf-8") as f:
                f.write(content)
            log("(فایل docker-compose.yml به حالت قبل بازگردانده شد.)")
        except OSError:
            pass
        raise DeployError(
            f"بالا آوردن کانتینر frontend با پورت جدید ناموفق بود:\n{err or out}",
            "\n".join(log_lines),
        )
    log("کانتینر frontend با پورت جدید بالا آمد.")
    log(f"از این پس پنل روی پورت {new_port} در دسترس است.")

    return "\n".join(log_lines)
