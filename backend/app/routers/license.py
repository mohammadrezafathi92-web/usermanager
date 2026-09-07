"""Superadmin-facing licence status + key entry.

Small on purpose. The panel's licence is enforced from services/
license_state.py; this router just lets the operator SEE the current state
and paste a new key. Recovering a LOCKED panel is not done here (you cannot
log in to a locked panel) - it is done on the vendor's side (un-revoke from
the console, or a fresh key in .env). This page is for the healthy case:
checking days remaining, entering the first key, reading the fingerprint to
send to the vendor.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException

from .. import models, schemas  # noqa: F401 - schemas kept for symmetry
from ..config import settings
from ..deps import require_superadmin
from ..services import license_state, licensing
from ..services.local_deploy import HOST_PROJECT_DIR

router = APIRouter(prefix="/api/license", tags=["license"], dependencies=[Depends(require_superadmin)])


@router.get("/status")
def status():
    """Everything the operator needs to see - and the fingerprint they send
    the vendor to have a key issued."""
    e = license_state.current_enforcement()
    payload = None
    try:
        if settings.license_key:
            p, _b, _s = licensing.parse_token(settings.license_key)
            payload = {
                "license_id": p.license_id,
                "customer": p.customer,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "bound_to_this_machine": bool(p.fingerprint),
            }
    except licensing.LicenseError:
        payload = None

    return {
        "has_key": bool(settings.license_key),
        "master_install": settings.license_master_install,
        "fingerprint": licensing.hardware_fingerprint(),
        "server_url": settings.license_server_url,
        "locked": e.lock_panel,
        "lock_bot": e.lock_bot,
        "cut_services": e.cut_services,
        "reason": e.reason,
        "message": e.message,
        "expires_in_days": e.days_left,
        "grace_days_left": e.grace_days_left,
        "license": payload,
    }


@router.post("/check-now")
def check_now():
    """Force a heartbeat right now instead of waiting for the next beat -
    used after the vendor un-revokes, so the operator sees it immediately."""
    license_state.heartbeat()
    return status()


@router.put("/key")
def set_key(body: dict):
    """Paste a new signed key. It is validated and persisted to the .env
    next to the panel so it survives restarts (the key lives in the
    environment, config.license_key). Rejected if it does not verify or is
    for a different machine - so a wrong paste is caught here, not at the
    next restart.

    Note: this works while the panel is UNLOCKED. A locked panel cannot be
    logged into, so it is recovered vendor-side, not here.
    """
    token = (body or {}).get("key", "").strip()
    if not token:
        raise HTTPException(400, "کلید لایسنس خالی است")

    status_obj = licensing.verify(token, fingerprint=licensing.hardware_fingerprint())
    if status_obj.reason == licensing.REASON_BAD_SIGNATURE:
        raise HTTPException(400, "امضای این کلید معتبر نیست")
    if status_obj.reason == licensing.REASON_WRONG_MACHINE:
        raise HTTPException(400, "این کلید برای سرور دیگری صادر شده است")
    if status_obj.reason == licensing.REASON_MALFORMED:
        raise HTTPException(400, "کلید خوانده نشد - دوباره کپی کنید")

    _persist_key_to_env(token)
    settings.license_key = token
    license_state.set_license_key(token)
    return status()


def _persist_key_to_env(token: str) -> None:
    """Write LICENSE_KEY into backend/.env so a restart keeps it. Best-effort;
    if the file cannot be written the key still applies for this process.

    Bug fixed 2026-09-07: this used to default to /app/.env, which is NOT
    the host's backend/.env - the Dockerfile never copies a .env into the
    image and docker-compose.yml never bind-mounts one there. Writing to it
    "worked" (the running container's own writable layer took the write,
    and settings.license_key was also updated in memory for this process),
    but the very next `docker compose up -d --force-recreate` - needed to
    pick up any OTHER new env var, e.g. USERMANAGER_LICENSE_PUBKEY - throws
    that writable layer away and starts a clean container, silently
    reverting LICENSE_KEY to whatever backend/.env on the HOST actually
    says. A panel that had a key pasted, then had licensing turned on right
    after, came back up with no key at all: REASON_MISSING, locked, and
    login itself refused before the operator could even reach this page
    again to re-paste it.
    HOST_PROJECT_DIR is bind-mounted into this container at the SAME path
    it has on the host (see docker-compose.yml's backend volumes and
    local_deploy.py's own docstring on exactly this trick, used there for
    the self-service port-change feature) - so {HOST_PROJECT_DIR}/backend/
    .env genuinely IS the host's file, not a copy, and survives any
    recreate or rebuild."""
    env_path = os.environ.get(
        "ENV_FILE_PATH", os.path.join(HOST_PROJECT_DIR, "backend", ".env")
    )
    try:
        lines = []
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as fh:
                lines = [ln for ln in fh.read().splitlines() if not ln.startswith("LICENSE_KEY=")]
        lines.append(f"LICENSE_KEY={token}")
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
