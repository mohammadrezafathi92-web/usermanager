"""OpenVPN / L2TP / IKEv2 / SSTP / PPTP (design v7.1, section 9.2).

These are authenticated by this panel's own RADIUS server against its own
database - there is nothing on a node to create, read or delete. The
adapter exists so the caller can treat every backend alike; each operation
makes no remote call at all."""
from __future__ import annotations

from .adapter_base import PresentResult

HAS_REMOTE = False


def ensure_present(*_args, **_kwargs) -> PresentResult:
    return PresentResult(created=False, notes=("no_remote",))


def ensure_absent(*_args, **_kwargs) -> None:
    """No remote, so no remote_outcome: the step goes straight to 'removed'."""
    return None
