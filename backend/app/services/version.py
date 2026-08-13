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


@functools.lru_cache(maxsize=1)
def get_build_info() -> dict:
    """Cached for the process lifetime - the code cannot change under a
    running container, and this is read on every /me call."""
    version = None
    commit = None

    for root in _CANDIDATE_ROOTS:
        # Any filesystem error here is non-fatal by design. A candidate path
        # can exist but be unreadable (a root-owned mount seen from a
        # different uid raises PermissionError on the is_dir() check alone),
        # and reporting a version is never worth refusing to start the panel.
        try:
            if version is None:
                version = _read_first(root / "VERSION")
            if commit is None:
                git_dir = root / ".git"
                if git_dir.is_dir():
                    commit = _resolve_commit(git_dir)
        except OSError:
            continue
        if version and commit:
            break

    # An explicit override wins over everything, for deployments that build
    # from a tarball with no repository present at all.
    version = os.environ.get("APP_VERSION") or version or DEFAULT_VERSION
    commit = os.environ.get("GIT_COMMIT") or commit

    return {
        "version": version,
        "commit": commit,
        "commit_short": commit[:7] if commit else None,
    }
