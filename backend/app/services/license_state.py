"""The panel side of licensing: hold the current verdict, persist the few
facts verify() needs across restarts, and run the heartbeat.

licensing.py is the pure decision; this is the stateful shell around it -
where the token, the last successful online check, the revoked flag and the
highest clock value seen actually live, and where the periodic heartbeat to
the vendor's control server happens.

Design (docs/licensing-design.md):
  * the verdict is CACHED and recomputed cheaply; the panel reads
    current_enforcement() on every gated request and it never blocks on the
    network.
  * the heartbeat runs on the scheduler, out of band. Its ONLY jobs are to
    learn "revoked?" and the chosen lock scope, and to stamp last_online.
  * silence does not lock by default - a control-server outage must not
    touch a paid customer (config.license_lock_after_silent_days = 0).
  * the vendor's own master install is never enforced.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

from ..config import settings
from . import licensing
from .version import get_build_info

logger = logging.getLogger("license")

# The handful of mutable facts persisted next to the database, so a restart
# does not reset the grace clock or forget a revocation. Deliberately a tiny
# JSON file, not a DB table: it must be readable even if the DB is the thing
# a licence problem is blocking.
_STATE_PATH = os.environ.get("LICENSE_STATE_PATH", "/app/data/.license_state.json")

_lock = threading.Lock()
_cached: Optional[licensing.Enforcement] = None


def _load_state() -> dict:
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _STATE_PATH)
    except OSError:
        logger.warning("could not persist licence state to %s", _STATE_PATH)


def _parse_dt(value) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _compute(now: Optional[dt.datetime] = None) -> licensing.Enforcement:
    """Recompute the verdict from the stored token + persisted facts."""
    now = now or dt.datetime.utcnow()

    # The vendor's own install is never enforced - it hosts the console and
    # must not be able to lock itself out.
    if settings.license_master_install:
        return licensing.Enforcement(reason=licensing.REASON_OK, message="master install")

    state = _load_state()

    # Track the highest clock value we have ever seen, so moving the clock
    # back to dodge an expiry is caught (licensing.verify's highest_seen).
    highest = _parse_dt(state.get("highest_seen_time"))
    if highest is None or now > highest:
        highest = now
        state["highest_seen_time"] = highest.replace(microsecond=0).isoformat()
        _save_state(state)

    status = licensing.verify(
        settings.license_key,
        now=now,
        last_online_check=_parse_dt(state.get("last_online_check")),
        revoked=bool(state.get("revoked", False)),
        highest_seen_time=highest,
        lock_after_silent_days=(settings.license_lock_after_silent_days or None),
    )
    scope = state.get("lock_scope") or licensing.DEFAULT_LOCK_SCOPE
    return licensing.resolve_enforcement(status, scope)


def refresh() -> licensing.Enforcement:
    """Recompute and cache. Cheap - no network."""
    global _cached
    with _lock:
        _cached = _compute()
    return _cached


def current_enforcement() -> licensing.Enforcement:
    """What the panel acts on. Never touches the network; returns the cache,
    computing it once on first use."""
    if _cached is None:
        return refresh()
    return _cached


# --------------------------------------------------------------------------
# the heartbeat
# --------------------------------------------------------------------------
def _license_id() -> Optional[str]:
    if not settings.license_key:
        return None
    try:
        payload, _b, _s = licensing.parse_token(settings.license_key)
        return payload.license_id
    except licensing.LicenseError:
        return None


def heartbeat(timeout: float = 10.0) -> None:
    """One check-in with the control server. Best-effort: any failure is
    logged and swallowed, and the panel keeps running on its cached verdict.

    A successful call learns revoked/lock_scope and stamps last_online. A
    failed one changes nothing - which is the whole point of silence not
    locking by default.
    """
    if settings.license_master_install:
        return
    license_id = _license_id()
    if not license_id or not settings.license_server_url:
        return

    payload = json.dumps({
        "license_id": license_id,
        "fingerprint": licensing.hardware_fingerprint(),
        "panel_version": get_build_info().get("version"),
    }).encode("utf-8")

    url = settings.license_server_url.rstrip("/") + "/heartbeat"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        logger.debug("licence heartbeat to %s failed: %s", url, exc)
        return

    state = _load_state()
    state["last_online_check"] = dt.datetime.utcnow().replace(microsecond=0).isoformat()
    if isinstance(data, dict):
        if "revoked" in data:
            state["revoked"] = bool(data["revoked"])
        scope = data.get("lock_scope")
        if scope in licensing.LOCK_SCOPES:
            state["lock_scope"] = scope
    _save_state(state)
    refresh()


def heartbeat_job() -> None:
    """Scheduler entry point - heartbeat, then log a one-line summary if the
    verdict is anything other than fine."""
    heartbeat()
    e = current_enforcement()
    if e.any_lock:
        logger.warning("licence enforcement active: %s (%s)", e.reason, e.message)


def set_license_key(token: str) -> licensing.Enforcement:
    """Called when the operator pastes a new key into the panel. The key
    itself lives in the environment/.env (config.license_key); this clears
    the cached revocation so a freshly-issued key gets a clean check, then
    recomputes."""
    state = _load_state()
    state.pop("revoked", None)
    _save_state(state)
    return refresh()
