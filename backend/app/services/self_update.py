"""«بروزرسانی پنل» - check for and queue a panel update from the panel itself.

check_for_update() fetches and reports what main would bring, without
touching the working tree. apply_update() no longer pulls, builds, resets or
restarts anything in this process: it validates the request and queues the
SAME safe update.sh the SSH path runs, in a pinned sibling container (see
services/panel_safe_update.py and scripts/panel_update_runner.py). That
worker shares update.sh's lock with the CLI, makes and verifies the backup,
deploys exactly the requested main SHA, builds before restarting, checks
health and compares the modes - and it outlives this container's
replacement. Server-side changes to tracked files are never discarded here.
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from .local_deploy import (
    HOST_PROJECT_DIR,
    DeployError,
    verify_host_path,
)

logger = logging.getLogger("self_update")

# A fetch should be near-instant unless the network is bad.
GIT_TIMEOUT = 120


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
        # This says "container", not "server", on purpose. git IS normally
        # installed on the host; what is missing is the binary inside this
        # image - a distinction that sent at least one admin looking in the
        # wrong place. Images built before git was added to the Dockerfile
        # cannot update themselves, so the way out is named here.
        return 127, (
            "git داخل کانتینر پنل نصب نیست (روی خود سرور احتمالا هست). "
            "این نسخه‌ی پنل قبل از اضافه‌شدن git ساخته شده، پس نمی‌تواند خودش را بروز کند. "
            "یک‌بار با SSH این را اجرا کنید و بعد از آن دکمه‌ی بروزرسانی کار می‌کند:\n"
            "cd /root/usermanager && git pull && docker compose up -d --build"
        )
    except subprocess.TimeoutExpired:
        return 124, "زمان اجرای git تمام شد"


def _porcelain_name(line: str) -> str:
    """Filename out of one `git status --porcelain` line, offset-free."""
    rest = line.strip().split(" ", 1)
    name = rest[1].strip() if len(rest) > 1 else rest[0].strip()
    # Renames read "old -> new"; the new path is the one that matters here.
    if " -> " in name:
        name = name.split(" -> ", 1)[1].strip()
    return name.strip('"')


def _repo_is_git() -> bool:
    """A tarball/zip install has no .git, so every command below would fail
    with git's own wording ("not a git repository"), which does not tell an
    admin what to do about it. Checked once, up front."""
    return os.path.isdir(os.path.join(HOST_PROJECT_DIR, ".git"))


NOT_A_CLONE = (
    "این نصب از فایل فشرده انجام شده و تاریخچه‌ی گیت ندارد، پس بروزرسانی خودکار ممکن نیست. "
    "یک‌بار با SSH پروژه را با git clone نصب کنید (راهنما در update.sh) تا این دکمه فعال شود."
)


def current_revision() -> dict:
    """What is checked out right now, plus whether anything newer exists."""
    _, local = _git("rev-parse", "--short", "HEAD")
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    # --untracked-files=no is the important part. A fast-forward pull does
    # not care about untracked files (git refuses on its own, with a clear
    # message, in the one case where it would overwrite one), yet the
    # previous check counted them - so a stray log, a leftover .zip, or a
    # mistyped scp target sitting in the directory was enough to disable
    # updates forever with a message about "manual changes" that named
    # nothing and was not even true.
    _, dirty = _git("status", "--porcelain", "--untracked-files=no")
    return {
        "commit": local.strip() or None,
        "branch": branch.strip() or None,
        # A dirty tree means someone edited files on the server by hand. The
        # pull would fail anyway, so it is surfaced BEFORE the admin presses
        # the button rather than as a confusing git error afterwards.
        "dirty": bool(dirty.strip()),
        # The list itself, so the admin can be told WHAT changed instead of
        # being sent to run git status over SSH.
        # NOT line[3:]. Porcelain lines start with a two-character status
        # and a space (" M file"), but _git strips the whole output, which
        # eats the leading space of the FIRST line only - so a fixed offset
        # silently ate the first letter of one filename and the comparison
        # against ["docker-compose.yml"] below never matched.
        "dirty_files": [_porcelain_name(line) for line in dirty.splitlines() if line.strip()],
    }


def check_for_update() -> dict:
    """Fetches from the remote WITHOUT changing the working tree, and reports
    how many commits behind this deployment is."""
    if not _repo_is_git():
        raise DeployError(NOT_A_CLONE, "")

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
    _, target = _git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}")
    return {
        **info,
        # the exact commit an update would deploy (the UI sends it back)
        "target": target.strip() if re.fullmatch(r"[0-9a-f]{40}", target.strip()) else None,
        "behind": count,
        "up_to_date": count == 0,
        # The actual commit subjects, so the admin can see WHAT the update
        # contains before applying it rather than trusting a number.
        "pending": [line for line in log.splitlines() if line.strip()][:20],
    }


def apply_update(target: str, actor_id) -> dict:
    """Queues the SAME safe update.sh the SSH path runs (services/
    panel_safe_update.py): shared lock with the CLI, verified backup, the
    exact target, build, health checks, modes compared. Nothing is pulled,
    reset, discarded or restarted in this process - the answer only says the
    job was queued; the UI follows the job's own state to its real end."""
    if not _repo_is_git():
        raise DeployError(NOT_A_CLONE, "")
    if not isinstance(target, str) or not re.fullmatch(r"[0-9a-f]{40}", target):
        raise DeployError("نسخهٔ مقصد باید SHA کامل ۴۰ کاراکتری main باشد", "")
    # Before anything is queued: if the daemon would resolve this project to
    # a different host directory than the one the worker is handed, stop.
    verify_host_path()
    info = current_revision()
    if info["dirty"]:
        # Never discarded here (the CLI refuses them the same way).
        raise DeployError(
            "روی سرور این فایل‌ها دستی تغییر کرده‌اند؛ آپدیت انجام نمی‌شود:\n"
            + "\n".join(f"• {f}" for f in (info.get("dirty_files") or [])[:15])
            + "\n\nتغییرات را خودتان روی سرور بررسی کنید؛ پنل آن‌ها را کنار نمی‌گذارد.",
            "",
        )
    if info["branch"] != "main":
        raise DeployError("آپدیت فقط روی شاخهٔ main انجام می‌شود", "")
    code, remote = _git("rev-parse", "--verify", "-q", "refs/remotes/origin/main")
    if code != 0 or remote.strip() != target:
        raise DeployError("نسخهٔ مقصد با آخرین main بررسی‌شده یکی نیست؛ دوباره «بررسی» بزنید", "")
    from . import panel_safe_update
    return panel_safe_update.queue(target, actor_id)
