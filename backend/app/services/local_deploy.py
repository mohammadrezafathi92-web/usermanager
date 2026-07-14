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


def change_panel_port_local(current_port: int, new_port: int) -> str:
    """Rewrites the frontend service's host-side port mapping in
    docker-compose.yml (only the exact "CURRENT:80" string, same
    conservative approach the old SSH version used - never a generic port
    regex) and recreates just that one container via the local docker
    socket. Raises DeployError with a Persian message on any failure."""
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
