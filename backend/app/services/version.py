"""What is actually running: a human release number plus the exact commit.

Before this, the panel's sidebar showed "نسخه ۱.۰" - a hardcoded string in
i18n/translations.js, wired to nothing, which had said 1.0 forever while
frontend/package.json separately claimed 1.2.0 and the repository had no git
tags at all. Three numbers, none of them true.

The commit id matters more than the release number in practice. The failure
it prevents is a real one from 2026-08-13: a mobile-layout fix was tested on
the live panel, appeared not to work, and was investigated - when in fact it
had simply never been deployed. A visible commit id answers "is my change
live?" in one glance.

Read at RUNTIME from the repository checkout rather than baked in as a build
argument, deliberately. A build arg records what someone remembered to pass
on the command line; reading .git records what is genuinely checked out on
the server. The repository is already bind-mounted into this container (see
docker-compose.yml's `.:/root/usermanager`, present for the panel-port
feature), and .git is parsed by hand - no `git` binary exists in the slim
image, and none is needed for this.

Everything degrades to None rather than raising: a bot-standalone deployment
or a source install without .git must still start normally, just without a
commit id to show.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

# Both locations are tried: the bind-mounted repository first (authoritative
# - it is the code that was actually pulled), then a copy baked into the
# image, then the source tree itself when running outside Docker.
_CANDIDATE_ROOTS = (
    # Wherever the repository is actually mounted - the compose file now
    # mounts it at the host's own path, so this is no longer always
    # /root/usermanager (install.sh picks /opt/usermanager on a fresh box).
    Path(os.environ.get("HOST_PROJECT_DIR", "/root/usermanager")),
    Path("/root/usermanager"),
    Path("/app"),
    Path(__file__).resolve().parents[3],
)

DEFAULT_VERSION = "0.0.0"


def _read_first(*paths: Path) -> str | None:
    for p in paths:
        try:
            text = p.read_text(encoding="utf-8").strip()
            if text:
                return text
        except (OSError, UnicodeDecodeError):
            continue
    return None


def _resolve_commit(git_dir: Path) -> str | None:
    """Follows HEAD to a sha without shelling out to git.

    Three cases, all of which occur in practice:
      - detached HEAD: the file holds the sha directly
      - normal branch: HEAD points at refs/heads/<branch>, a file with the sha
      - packed refs: after a `git gc` or a fresh clone the loose ref file may
        not exist and the sha lives in .git/packed-refs instead
    """
    head = _read_first(git_dir / "HEAD")
    if not head:
        return None
    if not head.startswith("ref:"):
        return head  # detached HEAD
    ref = head.split(" ", 1)[1].strip()

    sha = _read_first(git_dir / ref)
    if sha:
        return sha

    packed = _read_first(git_dir / "packed-refs")
    if packed:
        for line in packed.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split(" ", 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    return None


def _resolve_commit_count(root: Path) -> int | None:
    """How many commits are in this checkout's history - the auto-incrementing
    build number.

    Uses the git binary (which IS present in the image now - the Dockerfile
    installs it for services/self_update.py). Every pull adds commits, so
    this rises on its own with each update, with no VERSION file to bump by
    hand. Returns None if git is absent or the call fails, so the panel falls
    back to the plain VERSION string and never breaks over a version number.
    """
    import subprocess
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
            capture_output=True, text=True, timeout=3, check=True,
        )
        return int(out.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _auto_version(base: str | None, count: int | None) -> str | None:
    """MAJOR.MINOR from the VERSION file + the commit count as the patch.

    "1.3.0" + 167 commits -> "1.3.167". The VERSION file now only needs a
    MAJOR.MINOR that a human bumps when a release is meaningfully new; the
    third number rides the commit count automatically. If the count is not
    available, the raw VERSION string is used unchanged (old behaviour).
    """
    if not base:
        return None
    if count is None:
        return base
    parts = base.strip().split(".")
    major = parts[0] if len(parts) >= 1 and parts[0] else "0"
    minor = parts[1] if len(parts) >= 2 and parts[1] else "0"
    return f"{major}.{minor}.{count}"


@functools.lru_cache(maxsize=1)
def get_build_info() -> dict:
    """Cached for the process lifetime - the code cannot change under a
    running container, and this is read on every /me call (and now shells
    out to git for the commit count, which we do NOT want to repeat per
    request)."""
    base_version = None
    commit = None
    commit_count = None

    for root in _CANDIDATE_ROOTS:
        # Any filesystem error here is non-fatal by design. A candidate path
        # can exist but be unreadable (a root-owned mount seen from a
        # different uid raises PermissionError on the is_dir() check alone),
        # and reporting a version is never worth refusing to start the panel.
        try:
            if base_version is None:
                base_version = _read_first(root / "VERSION")
            git_dir = root / ".git"
            if git_dir.is_dir():
                if commit is None:
                    commit = _resolve_commit(git_dir)
                if commit_count is None:
                    commit_count = _resolve_commit_count(root)
        except OSError:
            continue
        if base_version and commit and commit_count is not None:
            break

    # The auto-incrementing version: MAJOR.MINOR from VERSION + commit count.
    version = _auto_version(base_version, commit_count)

    # An explicit override still wins over everything, for a tarball build
    # with no repository present at all.
    version = os.environ.get("APP_VERSION") or version or base_version or DEFAULT_VERSION
    commit = os.environ.get("GIT_COMMIT") or commit

    return {
        "version": version,
        "commit": commit,
        "commit_short": commit[:7] if commit else None,
    }
