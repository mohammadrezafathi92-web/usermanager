"""The panel side of licensing: the cached verdict, the login gate, and the
heartbeat's effect - without any real network or a real control server.

Run:  python3 backend/tests/test_license_enforcement.py

license_state.py is where the stateful shell around licensing.verify lives.
The things that must hold:
  - the master install is NEVER enforced (the vendor cannot lock themself
    out of the console);
  - a good licence lets login through; a locked one refuses it, with the
    reason;
  - a heartbeat that returns revoked flips the verdict, and one that just
    times out changes nothing (silence does not lock).
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.services import licensing, license_state

failures: list[str] = []
NOW = dt.datetime(2026, 8, 27, 12, 0, 0)
FP = licensing.hardware_fingerprint()  # this machine's real fingerprint


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# A signing key just for this test; wire its public half into licensing.
PRIVATE = Ed25519PrivateKey.generate()
licensing.SIGNING_PUBLIC_KEY_B64 = base64.urlsafe_b64encode(
    PRIVATE.public_key().public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)
).decode().rstrip("=")


def issue(*, days=365, fingerprint=FP):
    p = licensing.LicensePayload(
        license_id="lic_panel", customer="تست", fingerprint=fingerprint,
        issued_at=NOW - dt.timedelta(days=1),
        expires_at=(NOW + dt.timedelta(days=days)) if days is not None else None,
    )
    body = json.dumps(p.to_dict(), ensure_ascii=False, separators=(",", ":"),
                      sort_keys=True).encode()
    b64 = lambda r: base64.urlsafe_b64encode(r).decode().rstrip("=")
    return f"{licensing.TOKEN_PREFIX}.{b64(body)}.{b64(PRIVATE.sign(body))}"


def reset_state(**overrides):
    """Point license_state at a fresh temp state file and set config."""
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.unlink(path)
    license_state._STATE_PATH = path
    license_state._cached = None
    settings.license_master_install = overrides.get("master", False)
    settings.license_key = overrides.get("key", "")
    settings.license_lock_after_silent_days = overrides.get("silent_days", 0)
    return path


print("--- a healthy licence: nothing is locked ---")
reset_state(key=issue())
e = license_state.refresh()
check("panel not locked", e.lock_panel, False)
check("nothing cut", e.any_lock, False)

print("\n--- an expired licence locks the panel ---")
reset_state(key=issue(days=-1))
e = license_state.refresh()
check("panel locked", e.lock_panel, True)
check("reason is expiry", e.reason, licensing.REASON_EXPIRED)
check("message is customer-facing", "اعتبار" in e.message, True)

print("\n--- the MASTER install is never enforced ---")
reset_state(key=issue(days=-1), master=True)  # an expired key AND master
e = license_state.refresh()
check("master install ignores an expired key", e.lock_panel, False)
check("...reason is ok", e.reason, licensing.REASON_OK)

print("\n--- no public key compiled in = fails open (dev build) ---")
saved_pub = licensing.SIGNING_PUBLIC_KEY_B64
licensing.SIGNING_PUBLIC_KEY_B64 = ""
reset_state(key="anything")
check("a keyless build never locks", license_state.refresh().lock_panel, False)
licensing.SIGNING_PUBLIC_KEY_B64 = saved_pub

print("\n--- the login gate ---")
# Drive the real login() with a fake DB + admin, checking it refuses when
# the panel is locked and lets a good password through when it is not.
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models
from app.routers import auth as auth_router
from app.security import hash_password


class FakeForm:
    def __init__(self, username, password):
        self.username = username
        self.password = password


def make_db_with_admin():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.AdminUser(username="boss", hashed_password=hash_password("pw"),
                            is_superadmin=True))
    db.commit()
    return db


class FakeReq:
    headers = {}
    client = type("C", (), {"host": "1.2.3.4"})()


def try_login(db, password="pw"):
    try:
        auth_router._is_rate_limited = lambda *a, **k: False
        tok = auth_router.login(FakeReq(), FakeForm("boss", password), db=db)
        return ("ok", tok.access_token)
    except HTTPException as exc:
        return (exc.status_code, exc.detail)


reset_state(key=issue())
db = make_db_with_admin()
result = try_login(db)
check("valid licence: login succeeds", result[0], "ok")

check("a wrong password still fails first", try_login(db, "nope")[0], 401)

reset_state(key=issue(days=-1))
result = try_login(make_db_with_admin())
check("locked panel: login refused", result[0], 403)
check("...with the licence message", "اعتبار" in str(result[1]), True)

reset_state(key=issue(days=-1), master=True)
check("master install: login still works even with an expired key",
      try_login(make_db_with_admin())[0], "ok")

print("\n--- the heartbeat's effect on the verdict ---")
# Replace the network call with a stub returning whatever we want.
import urllib.request


class FakeResp:
    def __init__(self, payload):
        self._p = json.dumps(payload).encode()
    def read(self):
        return self._p
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def fake_urlopen(payload):
    def _open(req, timeout=10.0):
        return FakeResp(payload)
    return _open


reset_state(key=issue())
saved_open = urllib.request.urlopen

# A heartbeat that says "revoked" must lock the panel.
urllib.request.urlopen = fake_urlopen({"revoked": True, "lock_scope": "panel_only"})
license_state.heartbeat()
e = license_state.current_enforcement()
check("a revoking heartbeat locks the panel", e.lock_panel, True)
check("...for the revoked reason", e.reason, licensing.REASON_REVOKED)

# Then one that says "not revoked" must restore it.
urllib.request.urlopen = fake_urlopen({"revoked": False, "lock_scope": "panel_only"})
license_state.heartbeat()
check("un-revoking restores it", license_state.current_enforcement().lock_panel, False)

# A heartbeat that says "everything" scope, while revoked, cuts services too.
urllib.request.urlopen = fake_urlopen({"revoked": True, "lock_scope": "everything"})
license_state.heartbeat()
e = license_state.current_enforcement()
check("scope 'everything' from the server cuts services", e.cut_services, True)

# A heartbeat that TIMES OUT changes nothing - silence does not lock.
reset_state(key=issue())
license_state.refresh()

def boom(req, timeout=10.0):
    raise OSError("timeout")
urllib.request.urlopen = boom
license_state.heartbeat()
check("a failed heartbeat leaves the panel unlocked", license_state.current_enforcement().lock_panel, False)

urllib.request.urlopen = saved_open

print("\n--- clock rollback is caught ---")
reset_state(key=issue())
# Record a far-future highest_seen, then compute at NOW -> clock looks
# rolled back.
state = {"highest_seen_time": (NOW + dt.timedelta(days=60)).isoformat()}
with open(license_state._STATE_PATH, "w") as fh:
    json.dump(state, fh)
license_state._cached = None
e = license_state._compute(now=NOW)
check("a clock far behind what we have seen locks", e.lock_panel, True)
check("...for the clock reason", e.reason, licensing.REASON_CLOCK)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
