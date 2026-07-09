"""Automates installing the INTERACTIVE Telegram bot on a second server via
SSH - "نصب ربات روی سرور دیگر" in Settings. Ships the exact same backend
image the mother panel already runs (the panel and the bot share one
codebase - see telegram_bot/panel_bridge.py's module docstring) but starts
it in BOT_STANDALONE_MODE, pointed back at THIS server's real database over
`/api/bot/*` (X-API-Key) instead of a local one - see
telegram_bot/remote_bridge.py and main.py's on_startup().

Why reuse the full image instead of building a slim bot-only one:
telegram_bot/panel_bridge.py imports the full routers/models package at
module load time (even though those code paths go unused in remote mode),
so the full dependency set has to be installed on the target server
regardless. Reusing the exact image the mother server runs means zero
extra Docker images to maintain and a source tree that's always in sync by
construction - the two servers just differ in environment variables.

Security note: the SSH password is only ever held in memory for the
duration of one deploy/stop call (see routers/remote_bot.py) - it is never
written to the database or to disk. Host keys are auto-accepted
(AutoAddPolicy) since this is a first-contact automated setup flow, same
tradeoff any "give me your server and I'll SSH in" tool makes."""
from __future__ import annotations

import io
import logging
import os
import socket
import tarfile
import time

import paramiko

logger = logging.getLogger("remote_deploy")

# services/remote_deploy.py -> app/services -> app -> /app (the Dockerfile's WORKDIR)
APP_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
REMOTE_DIR = "/root/usermanager-bot"

DOCKERFILE = """FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
"""

COMPOSE_YML = """version: "3.8"

services:
  bot:
    build: .
    container_name: usermanager-remote-bot
    restart: unless-stopped
    env_file:
      - ./.env
    volumes:
      - ./data:/app/data
"""


class DeployError(Exception):
    """Raised on any failed step - .log holds whatever progress lines were
    captured before the failure, so the panel can show the admin exactly
    how far it got."""

    def __init__(self, message: str, log: str = ""):
        super().__init__(message)
        self.log = log


def _run(client: paramiko.SSHClient, cmd: str, timeout: int = 60):
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    exit_code = stdout.channel.recv_exit_status()
    out = stdout.read().decode("utf-8", "replace")
    err = stderr.read().decode("utf-8", "replace")
    return exit_code, out, err


def _build_app_tar() -> bytes:
    """Tars up this container's own /app/app directory (the exact same
    source the mother server runs right now) - excludes __pycache__/data so
    the upload stays small and never leaks the mother server's own
    database or uploaded files."""
    buf = io.BytesIO()
    app_src = os.path.join(APP_DIR, "app")
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for root, dirs, files in os.walk(app_src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for fname in files:
                if fname.endswith(".pyc"):
                    continue
                full = os.path.join(root, fname)
                arcname = os.path.join("app", os.path.relpath(full, app_src))
                tar.add(full, arcname=arcname)
    return buf.getvalue()


def _build_env(panel_api_url: str, panel_api_key: str, bot_token: str, admin_ids: str, approval_chat_ids: str) -> str:
    return (
        "DATABASE_URL=sqlite:////app/data/usermanager.db\n"
        "BOT_STANDALONE_MODE=true\n"
        f"PANEL_API_URL={panel_api_url}\n"
        f"PANEL_API_KEY={panel_api_key}\n"
        f"BOT_TOKEN={bot_token}\n"
        f"BOT_ADMIN_IDS={admin_ids}\n"
        f"BOT_APPROVAL_CHAT_IDS={approval_chat_ids}\n"
        "RADIUS_ENABLED=false\n"
    )


def _connect(host: str, ssh_port: int, ssh_username: str, ssh_password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host,
            port=ssh_port,
            username=ssh_username,
            password=ssh_password,
            timeout=20,
            banner_timeout=20,
            auth_timeout=20,
        )
    except Exception as exc:
        raise DeployError(f"اتصال SSH به {host}:{ssh_port} ناموفق بود: {exc}") from exc
    return client


def deploy(
    host: str,
    ssh_port: int,
    ssh_username: str,
    ssh_password: str,
    panel_api_url: str,
    panel_api_key: str,
    bot_token: str,
    admin_ids: str,
    approval_chat_ids: str,
) -> str:
    """Connects over SSH, installs Docker if missing, uploads the backend
    source plus a bot-only docker-compose setup, and brings it up. Returns
    a human-readable progress log on success; raises DeployError (carrying
    whatever log was captured so far) on failure."""
    log_lines: list[str] = []

    def log(line: str) -> None:
        log_lines.append(line)
        logger.info(line)

    log(f"در حال اتصال SSH به {host}:{ssh_port} ...")
    client = _connect(host, ssh_port, ssh_username, ssh_password)
    log("اتصال SSH برقرار شد.")

    try:
        log("بررسی نصب Docker ...")
        code, _, _ = _run(client, "command -v docker", timeout=15)
        if code != 0:
            log("Docker پیدا نشد - در حال نصب (فقط سیستم‌عامل‌های مبتنی بر Debian/Ubuntu پشتیبانی می‌شود) ...")
            code, out, err = _run(
                client,
                "apt-get update -y && apt-get install -y docker.io docker-compose-plugin",
                timeout=300,
            )
            if code != 0:
                raise DeployError(f"نصب Docker ناموفق بود:\n{err or out}", "\n".join(log_lines))
            log("Docker نصب شد.")
        else:
            log("Docker از قبل نصب است.")

        code, out, err = _run(client, "docker compose version", timeout=15)
        if code != 0:
            raise DeployError(f"پلاگین docker compose در دسترس نیست:\n{err or out}", "\n".join(log_lines))

        log("در حال آماده‌سازی پوشه مقصد ...")
        _run(client, f"mkdir -p {REMOTE_DIR}/data", timeout=15)

        log("در حال ارسال فایل‌های برنامه ...")
        sftp = client.open_sftp()
        try:
            with sftp.open(f"{REMOTE_DIR}/app.tar.gz", "wb") as f:
                f.write(_build_app_tar())

            with open(os.path.join(APP_DIR, "requirements.txt"), "rb") as f:
                requirements_content = f.read()
            with sftp.open(f"{REMOTE_DIR}/requirements.txt", "wb") as f:
                f.write(requirements_content)

            with sftp.open(f"{REMOTE_DIR}/Dockerfile", "wb") as f:
                f.write(DOCKERFILE.encode("utf-8"))
            with sftp.open(f"{REMOTE_DIR}/docker-compose.yml", "wb") as f:
                f.write(COMPOSE_YML.encode("utf-8"))

            env_content = _build_env(panel_api_url, panel_api_key, bot_token, admin_ids, approval_chat_ids)
            with sftp.open(f"{REMOTE_DIR}/.env", "wb") as f:
                f.write(env_content.encode("utf-8"))
            # .env holds the panel API key + bot token in plaintext -
            # restrict it to the owner right away instead of leaving it
            # world-readable (SFTP writes typically land at the server's
            # default umask, e.g. 644) for however long until someone
            # thinks to chmod it by hand.
            sftp.chmod(f"{REMOTE_DIR}/.env", 0o600)
        finally:
            sftp.close()
        log("فایل‌ها ارسال شدند.")

        log("در حال استخراج فایل‌ها ...")
        code, out, err = _run(client, f"cd {REMOTE_DIR} && tar xzf app.tar.gz && rm -f app.tar.gz", timeout=30)
        if code != 0:
            raise DeployError(f"استخراج فایل‌ها ناموفق بود:\n{err or out}", "\n".join(log_lines))

        log("در حال ساخت و اجرای کانتینر ربات (ممکن است چند دقیقه طول بکشد) ...")
        code, out, err = _run(client, f"cd {REMOTE_DIR} && docker compose up -d --build", timeout=600)
        if code != 0:
            raise DeployError(f"اجرای کانتینر ناموفق بود:\n{err or out}", "\n".join(log_lines))
        log("کانتینر با موفقیت اجرا شد.")

        log("در حال بررسی وضعیت ربات ...")
        time.sleep(4)
        _, out, err = _run(client, f"cd {REMOTE_DIR} && docker compose logs --tail 30 bot", timeout=20)
        log("--- گزارش راه‌اندازی ربات ---")
        log((out or err or "").strip() or "(خروجی خالی)")

        return "\n".join(log_lines)
    except socket.timeout as exc:
        # A hung remote command (e.g. `docker compose up --build` stuck on a
        # slow image pull) previously surfaced as a raw socket.timeout
        # instead of DeployError, losing the accumulated progress log the
        # UI is built to show.
        raise DeployError(f"عملیات با تایم‌اوت مواجه شد: {exc}", "\n".join(log_lines)) from exc
    finally:
        client.close()


def stop(host: str, ssh_port: int, ssh_username: str, ssh_password: str) -> str:
    """SSHes in and brings the remote bot container down - used when the
    admin clicks "بازگرداندن ربات به همین سرور"."""
    client = _connect(host, ssh_port, ssh_username, ssh_password)
    try:
        code, out, err = _run(client, f"cd {REMOTE_DIR} && docker compose down", timeout=60)
        if code != 0:
            raise DeployError(f"متوقف کردن ربات روی سرور دور ناموفق بود:\n{err or out}")
        return out.strip() or err.strip() or "کانتینر متوقف شد."
    finally:
        client.close()
