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

3. The restart kills this process - and the recreate command itself must
   NOT run as a subprocess of this process, because it too lives inside the
   container about to be replaced. `docker compose up -d` issued that way
   gets killed mid-recreate the moment the daemon stops this container,
   leaving a container that never finished coming down - and the NEXT
   update then fails with "name already in use" (this happened for real on
   2026-08-2x and needed a manual `docker rm -f` over SSH). So this last
   step is handed to a throwaway SIBLING container instead (see
   local_deploy.spawn_sibling_container) - it shares the docker socket and
   the project mount but is not part of the compose project being
   recreated, so it survives this container's death and finishes the job.
   The frontend polls the version endpoint to find out when the new panel
   is up; anything else looks to the admin like the update "hung".
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time

from .local_deploy import (
    HOST_PROJECT_DIR,
    DeployError,
    ensure_docker_compose_cli,
    spawn_sibling_container,
    verify_host_path,
)

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


_COMPOSE_PORT_RE = re.compile(r'"(\d+):80"')


def _compose_path() -> str:
    return os.path.join(HOST_PROJECT_DIR, "docker-compose.yml")


def _frontend_port() -> str | None:
    """The host-side port the frontend is published on, read straight from
    docker-compose.yml - the same single mapping local_deploy.py rewrites."""
    try:
        with open(_compose_path(), encoding="utf-8") as f:
            match = _COMPOSE_PORT_RE.search(f.read())
    except OSError:
        return None
    return match.group(1) if match else None


def _restore_frontend_port(port: str) -> None:
    """Re-applies a custom panel port to the freshly pulled compose file.

    Failure here is logged, never raised: the update itself has already
    succeeded at this point, and the worst case is that the panel comes back
    on the default port - recoverable from the panel's own port setting,
    unlike a half-finished update.
    """
    try:
        path = _compose_path()
        with open(path, encoding="utf-8") as f:
            content = f.read()
        match = _COMPOSE_PORT_RE.search(content)
        if not match:
            logger.warning("بروزرسانی: نگاشت پورت در docker-compose.yml جدید پیدا نشد - پورت %s اعمال نشد", port)
            return
        if match.group(1) == port:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(match.group(0), f'"{port}:80"', 1))
        logger.info("بروزرسانی: پورت پنل %s دوباره اعمال شد", port)
    except OSError:
        logger.exception("بروزرسانی: اعمال دوباره‌ی پورت پنل ناموفق بود")


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
    if not _repo_is_git():
        raise DeployError(NOT_A_CLONE, "")

    # Before anything is pulled or built: if the daemon would resolve this
    # project to a different host directory than the one we are about to
    # hand it, stop here. See local_deploy.verify_host_path.
    verify_host_path()

    info = current_revision()
    # docker-compose.yml is a special case, and the reason this whole branch
    # exists: the panel's OWN "پورت پنل وب" feature rewrites that tracked
    # file (services/local_deploy.change_panel_port_local). So an admin who
    # ever changed the port from the panel had permanently disabled the
    # panel's update button - one feature silently switching another off.
    # The port is preserved across the pull rather than the update refused.
    saved_port = None
    dirty_files = list(info.get("dirty_files") or [])
    if dirty_files == ["docker-compose.yml"]:
        saved_port = _frontend_port()
        code, out = _git("checkout", "--", "docker-compose.yml")
        if code != 0:
            raise DeployError("بازگرداندن docker-compose.yml ناموفق بود", out)
        logger.info("بروزرسانی: تغییر پورت پنل (%s) کنار گذاشته شد تا بعد از دریافت کد دوباره اعمال شود", saved_port)
        info = current_revision()

    if info["dirty"]:
        raise DeployError(
            "روی سرور این فایل‌ها دستی تغییر کرده‌اند و بروزرسانی روی آن‌ها می‌نویسد:\n"
            + "\n".join(f"• {f}" for f in (info.get("dirty_files") or [])[:15])
            + "\n\nاگر تغییرات لازم نیستند، با SSH این را اجرا کنید و دوباره امتحان کنید:\n"
            "cd /root/usermanager && git checkout -- .",
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

    if saved_port:
        _restore_frontend_port(saved_port)

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
        logger.info("بروزرسانی: در حال راه‌اندازی مجدد سرویس‌ها (از طریق کانتینر کمکی)")
        try:
            # ensure_docker_compose_cli() downloads/caches the binary and
            # returns its path AS SEEN FROM THIS CONTAINER (/app/data/...).
            # The sibling container below does not have that /app/data
            # mount - it only mounts the project directory at HOST_PROJECT_DIR
            # (same convention docker-compose.yml itself uses elsewhere) - so
            # the SAME file has to be addressed by its path under that mount
            # instead: ./backend/data/.docker-cli/docker-compose, which is
            # exactly where docker-compose.yml's `./backend/data:/app/data`
            # bind puts it on the host.
            ensure_docker_compose_cli()
            compose_bin_for_sibling = os.path.join(
                HOST_PROJECT_DIR, "backend", "data", ".docker-cli", "docker-compose")
            log_path = os.path.join(HOST_PROJECT_DIR, "backend", "data", ".self_update_restart.log")
            spawn_sibling_container(
                [compose_bin_for_sibling, "-f", os.path.join(HOST_PROJECT_DIR, "docker-compose.yml"),
                 "up", "-d", "--remove-orphans"],
                log_path=log_path,
            )
        except DeployError as exc:
            logger.error("بروزرسانی: راه‌اندازی مجدد ناموفق بود: %s (%s)", exc, exc.log)
        except Exception:
            logger.exception("بروزرسانی: راه‌اندازی مجدد با خطای غیرمنتظره مواجه شد")

    threading.Thread(target=_restart_soon, daemon=True).start()

    return {
        "updated": True,
        "from": before,
        "commit": after,
        "message": "نسخه‌ی جدید ساخته شد و سرویس در حال راه‌اندازی مجدد است",
    }
