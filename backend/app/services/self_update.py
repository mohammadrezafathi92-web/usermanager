"""«بروزرسانی پنل» - pull the latest code from GitHub and rebuild, from the
panel itself instead of over SSH.

Reuses the plumbing services/local_deploy.py already established for the
panel-port feature: the repository is bind-mounted into this container at
the SAME absolute path the host's docker daemon sees, and that daemon is
reachable over its socket. See local_deploy's module docstring for why both
of those are required.

The design point that matters most here is that this runs INSIDE the very
container it is about to replace. Three consequences shaped the code:

1. `git pull` happens first and is checked on its own. If the pull fails
   (no network, a conflict, a dirty tree) nothing has been rebuilt and the
   running panel is untouched - the admin gets an error and keeps working.

2. The build runs BEFORE anything is restarted. A failed build leaves the
   old images in place and the old containers running, so a syntax error in
   a new commit cannot take the panel down. Only a successful build gets as
   far as `up -d`.

3. The restart kills this process. The HTTP response can therefore never be
   delivered, so `up -d` is deliberately fired in the background after the
   response is already on its way, and the frontend polls the version
   endpoint to find out when the new panel is up. Anything else looks to the
   admin like the update "hung".
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from .local_deploy import HOST_PROJECT_DIR, DeployError, ensure_docker_compose_cli

logger = logging.getLogger("self_update")

# A build with no cache can take a couple of minutes on a small VPS; a pull
# should be near-instant unless the network is bad.
GIT_TIMEOUT = 120
BUILD_TIMEOUT = 900


def _git(*args: str, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Runs git against the bind-mounted repository. Output is merged so a
    failure can be shown to the admin exactly as git reported it - these
    messages ("Your local changes would be overwritten", "Could not resolve
    host") are the whole diagnostic value."""
    try:
        proc = subprocess.run(
            ["git", "-C", HOST_PROJECT_DIR, *args],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, "git روی این سرور نصب نیست"
    except subprocess.TimeoutExpired:
        return 124, "زمان اجرای git تمام شد"


def current_revision() -> dict:
    """What is checked out right now, plus whether anything newer exists."""
    _, local = _git("rev-parse", "--short", "HEAD")
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    _, dirty = _git("status", "--porcelain")
    return {
        "commit": local.strip() or None,
        "branch": branch.strip() or None,
        # A dirty tree means someone edited files on the server by hand. The
        # pull would fail anyway, so it is surfaced BEFORE the admin presses
        # the button rather than as a confusing git error afterwards.
        "dirty": bool(dirty.strip()),
    }


def check_for_update() -> dict:
    """Fetches from the remote WITHOUT changing the working tree, and reports
    how many commits behind this deployment is."""
    code, out = _git("fetch", "--quiet", "origin")
    if code != 0:
        raise DeployError("دریافت اطلاعات از گیت‌هاب ناموفق بود", out)

    info = current_revision()
    branch = info["branch"] or "main"
    code, behind = _git("rev-list", "--count", f"HEAD..origin/{branch}")
    if code != 0:
        raise DeployError("مقایسه با نسخه‌ی گیت‌هاب ناموفق بود", behind)

    code, log = _git("log", "--oneline", f"HEAD..origin/{branch}")
    count = int(behind.strip() or 0)
    return {
        **info,
        "behind": count,
        "up_to_date": count == 0,
        # The actual commit subjects, so the admin can see WHAT the update
        # contains before applying it rather than trusting a number.
        "pending": [line for line in log.splitlines() if line.strip()][:20],
    }


def _compose(*args: str, timeout: int) -> tuple[int, str]:
    binary = ensure_docker_compose_cli()
    try:
        proc = subprocess.run(
            [binary, "-f", os.path.join(HOST_PROJECT_DIR, "docker-compose.yml"), *args],
            cwd=HOST_PROJECT_DIR, capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, "زمان اجرای docker compose تمام شد"


def apply_update() -> dict:
    """Pull, build, then restart in the background. Raises DeployError with
    the captured log if either of the first two steps fails - in which case
    the running panel has not been touched."""
    info = current_revision()
    if info["dirty"]:
        raise DeployError(
            "روی سرور فایل‌هایی دستی تغییر کرده‌اند و بروزرسانی روی آن‌ها می‌نویسد. "
            "اول با SSH آن‌ها را بررسی کنید (git status).",
            "",
        )

    before = info["commit"]
    branch = info["branch"] or "main"

    code, pull_log = _git("pull", "--ff-only", "origin", branch)
    if code != 0:
        # --ff-only rather than a merge: this is a deployment, not a place to
        # resolve history. A non-fast-forward means the server has diverged
        # and needs a human, not an automatic merge commit.
        raise DeployError("دریافت کد جدید ناموفق بود", pull_log)

    after = current_revision()["commit"]
    if after == before:
        return {"updated": False, "commit": after, "message": "همین حالا آخرین نسخه است"}

    # Build first, restart second - a broken commit must not be able to take
    # the panel down, and a failed build here leaves everything running.
    code, build_log = _compose("build", timeout=BUILD_TIMEOUT)
    if code != 0:
        _git("reset", "--hard", before or "HEAD@{1}")
        raise DeployError(
            "ساخت نسخه‌ی جدید ناموفق بود - پنل روی نسخه‌ی قبلی باقی ماند و کد هم برگردانده شد.",
            build_log[-4000:],
        )

    def _restart_soon() -> None:
        # The restart terminates this process, so it has to happen after the
        # HTTP response has been flushed. A couple of seconds is plenty and
        # keeps the admin from seeing a dead connection instead of a result.
        time.sleep(2)
        logger.info("بروزرسانی: در حال راه‌اندازی مجدد سرویس‌ها")
        code, log = _compose("up", "-d", timeout=BUILD_TIMEOUT)
        if code != 0:
            logger.error("بروزرسانی: راه‌اندازی مجدد ناموفق بود: %s", log[-2000:])

    threading.Thread(target=_restart_soon, daemon=True).start()

    return {
        "updated": True,
        "from": before,
        "commit": after,
        "message": "نسخه‌ی جدید ساخته شد و سرویس در حال راه‌اندازی مجدد است",
    }
