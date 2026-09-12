"""Superadmin-facing licence status + key entry.

Small on purpose. The panel's licence is enforced from services/
license_state.py; this router just lets the operator SEE the current state
and paste a new key.

Two entry points, because a licence-locked panel refuses logins and the
key form used to sit BEHIND that login - a fresh install with no licence
had no way in at all, which is the opposite of what a first-run activation
should be ("پنل رو هر بار بعد نصب باز کنه و بگه که لایسنس نداره و بتونه
لایسنس رو وارد کنه", 2026-09):

  * PUT /key       - the healthy case, needs a normal session.
  * POST /activate - the locked case. Unauthenticated as a ROUTE, but it
    takes the superadmin's own username and password in the body and
    verifies them itself, so it grants exactly what a login would have -
    no more. It is the only licence route that works while locked.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas  # noqa: F401 - schemas kept for symmetry
from ..config import settings
from ..database import get_db
from ..deps import require_superadmin
from ..security import verify_password
from ..services import license_state, licensing
from ..services.local_deploy import HOST_PROJECT_DIR

router = APIRouter(prefix="/api/license", tags=["license"], dependencies=[Depends(require_superadmin)])

# Deliberately NOT under the router above: that one requires a session, and
# the whole point of this one is that there is no way to get a session yet.
public_router = APIRouter(prefix="/api/license", tags=["license"])


@public_router.get("/state")
def public_state():
    """Whether this panel is licence-locked, and why - readable without a
    session so the login screen can explain itself instead of just showing
    a 403. Says nothing about WHO can log in; only about the licence."""
    e = license_state.current_enforcement()
    return {
        "locked": e.lock_panel,
        "reason": e.reason,
        "message": e.message,
        "fingerprint": licensing.hardware_fingerprint(),
    }


@public_router.post("/activate")
def activate(body: dict, db: Session = Depends(get_db)):
    """Install the first licence key on a panel that is locked for not
    having one.

    Requires the superadmin's username and password in the body - the same
    credentials a login needs - so this opens nothing that logging in would
    not have opened. It exists only because the licence lock sits on login
    itself, which would otherwise make a fresh install unrecoverable from
    the browser.
    """
    username = (body or {}).get("username", "").strip()
    password = (body or {}).get("password", "")
    token = (body or {}).get("key", "").strip()
    if not username or not password:
        raise HTTPException(400, "نام کاربری و رمز عبور لازم است")
    if not token:
        raise HTTPException(400, "کلید لایسنس خالی است")

    admin = db.query(models.AdminUser).filter(models.AdminUser.username == username).first()
    # Same answer for a wrong name and a wrong password, so this cannot be
    # used to discover usernames.
    if admin is None or not admin.is_superadmin or not verify_password(password, admin.hashed_password):
        raise HTTPException(401, "نام کاربری یا رمز عبور اشتباه است")

    _install_key(token)
    return {"ok": True, **status()}


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
    _install_key(token)
    return status()


def _install_key(token: str) -> None:
    """Validate a pasted key and make it this panel's licence. Shared by the
    logged-in PUT and the locked-panel activate, so the two can never drift
    into accepting different things."""
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


@router.delete("/key")
def delete_key():
    """Removes the current license key - the in-panel counterpart to
    stripping LICENSE_KEY out of backend/.env by hand over SSH, added
    2026-09-07 after doing exactly that manually, repeatedly, while
    activating licensing across several panels (wrong fingerprint pasted,
    re-issuing for a different machine, etc).

    Deliberately does NOT touch USERMANAGER_LICENSE_PUBKEY. If that is
    already set on this build, removing the key immediately locks the
    panel the normal way (REASON_MISSING) - same as any other missing-key
    state, and just as real as it would be over SSH. The frontend confirms
    this with the operator before calling here; nothing server-side
    softens it, because a locked panel refusing login afterward is the
    expected, honest consequence, not a bug to paper over."""
    _remove_key_from_env()
    settings.license_key = ""
    license_state.set_license_key("")
    return status()


def _env_path() -> str:
    return os.environ.get(
        "ENV_FILE_PATH", os.path.join(HOST_PROJECT_DIR, "backend", ".env")
    )


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
    env_path = _env_path()
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


def _remove_key_from_env() -> None:
    """The inverse of _persist_key_to_env - strips LICENSE_KEY out of the
    same real host file entirely, so a later restart or recreate does not
    resurrect a key the operator just removed from the panel."""
    env_path = _env_path()
    try:
        if not os.path.exists(env_path):
            return
        with open(env_path, "r", encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if not ln.startswith("LICENSE_KEY=")]
        with open(env_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
    except OSError:
        pass
