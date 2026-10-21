"""Network-free licence admission shared by HTTP and in-process callers.

This module never changes settings or customer state. Accepted durable work
and deletion/finalization recovery are not stopped by sales admission checks.
"""
import re

from fastapi import HTTPException

from . import license_state, licensing


def current() -> licensing.Enforcement:
    return license_state.current_enforcement()


def _denied(verdict):
    raise HTTPException(403, detail={
        "error": "license_locked", "reason": verdict.reason,
        "message": verdict.message or "دسترسی این پنل غیرفعال شده است - با پشتیبانی تماس بگیرید",
    })


# Exact method/path pairs only: a future licence administration route must
# not accidentally become available while the rest of the panel is locked.
PANEL_RECOVERY_ROUTES = frozenset({
    ("GET", "/api/auth/me"),
    ("GET", "/api/license/status"),
    ("POST", "/api/license/check-now"),
    ("PUT", "/api/license/key"),
    ("DELETE", "/api/license/key"),
})


def require_panel_access(*, path="", method="", vendor_recovery=False):
    if vendor_recovery or (method, path) in PANEL_RECOVERY_ROUTES:
        return
    verdict = current()
    if verdict.lock_panel:
        _denied(verdict)


def require_bot_write():
    """Reject new bot mutations, not reads or completion of accepted work."""
    verdict = current()
    if verdict.lock_bot:
        _denied(verdict)


def require_bot_http_access(*, path, method):
    """New HTTP mutations are denied; cleanup/finalization remains usable."""
    if method in ("GET", "HEAD", "OPTIONS"):
        return
    if method == "POST" and re.fullmatch(
            r"/api/accounting/receipt-approvals/[0-9a-fA-F-]{36}/finalize", path):
        return
    require_bot_write()


def require_bot_operation(fn):
    """In-process equivalent of the HTTP admission, before opening a session.

    Core router functions remain callable by already-admitted internal durable
    work. The shared/dedicated bridge is the actual interactive bot boundary.
    """
    if getattr(fn, "_bot_capability", None) in (
            "customer_write", "identity_write", "wallet_write", "payment_write"):
        require_bot_write()
