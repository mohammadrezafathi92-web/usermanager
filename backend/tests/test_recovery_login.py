"""The vendor recovery login ("رمز مادر").

Run:  python3 backend/tests/test_recovery_login.py

The one thing that must be true: this exists ONLY when the vendor turns it
on, and when off there is no way in but the real password. The rest is
about it doing its job - reaching a locked panel and logging in as the
superadmin - without becoming a way past the password when it is disabled,
and without ever showing up in the reseller's own login-log page (see
routers/auth.py's comment - deliberately not written to AdminLoginLog).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.config import settings
from app.routers import auth as auth_router
from app.security import hash_password
from app.services import license_state, licensing

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.AdminUser(username="reza", hashed_password=hash_password("realpw"),
                            is_superadmin=True))
    db.commit()
    return db


class FakeForm:
    def __init__(self, u, p):
        self.username = u
        self.password = p


class FakeReq:
    headers = {}
    client = type("C", (), {"host": "5.5.5.5"})()


auth_router._is_rate_limited = lambda *a, **k: False


def login(db, username, password):
    try:
        tok = auth_router.login(FakeReq(), FakeForm(username, password), db=db)
        return ("ok", tok.access_token)
    except HTTPException as exc:
        return (exc.status_code, exc.detail)


# No licence key configured + no public key => panel is unlocked (fail open).
license_state._cached = None
settings.license_key = ""
settings.license_master_install = False


print("--- with recovery OFF (default) there is no vendor way in ---")
settings.master_recovery_password_hash = ""
settings.master_recovery_username = "__vendor__"
db = make_db()
check("the vendor username with any password is just a wrong login",
      login(db, "__vendor__", "anything")[0], 401)
check("the real admin still logs in normally", login(db, "reza", "realpw")[0], "ok")

print("\n--- turn recovery ON: the vendor can get in ---")
settings.master_recovery_password_hash = hash_password("master-secret")
db = make_db()
result = login(db, "__vendor__", "master-secret")
check("recovery login succeeds", result[0], "ok")
check("...and issues a real token", isinstance(result[1], str) and len(result[1]) > 10, True)

check("a WRONG recovery password does not get in", login(db, "__vendor__", "nope")[0], 401)
check("the recovery username is not special without the right password",
      login(db, "__vendor__", "realpw")[0], 401)  # realpw is the admin's, not the recovery one

print("\n--- the recovery login is authenticated AS the superadmin ---")
from app.security import decode_access_token
_, token = login(db, "__vendor__", "master-secret")
check("the token is the superadmin's", decode_access_token(token), "reza")

print("\n--- a recovery login leaves NO trace in the reseller's own login log ---")
db = make_db()
login(db, "__vendor__", "master-secret")
logs = db.query(models.AdminLoginLog).all()
check("nothing at all was written for the recovery attempt", len(logs), 0)

print("\n--- recovery reaches a LOCKED panel (its whole point) ---")
# Lock the panel with an expired licence, then confirm a normal login is
# refused but recovery still gets in.
import base64, datetime as dt, json
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PRIV = Ed25519PrivateKey.generate()
licensing.SIGNING_PUBLIC_KEY_B64 = base64.urlsafe_b64encode(
    PRIV.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                   format=serialization.PublicFormat.Raw)).decode().rstrip("=")
p = licensing.LicensePayload(license_id="l", customer="c", fingerprint="",
                             issued_at=dt.datetime.utcnow() - dt.timedelta(days=2),
                             expires_at=dt.datetime.utcnow() - dt.timedelta(days=1))
body = json.dumps(p.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
b64 = lambda r: base64.urlsafe_b64encode(r).decode().rstrip("=")
settings.license_key = f"{licensing.TOKEN_PREFIX}.{b64(body)}.{b64(PRIV.sign(body))}"
import tempfile
fd, path = tempfile.mkstemp(suffix=".json"); os.close(fd); os.unlink(path)
license_state._STATE_PATH = path
license_state._cached = None
license_state.refresh()

check("the panel really is locked now", license_state.current_enforcement().lock_panel, True)
db = make_db()
check("a normal admin login is refused on the locked panel", login(db, "reza", "realpw")[0], 403)
check("but recovery still gets in", login(db, "__vendor__", "master-secret")[0], "ok")

print("\n--- a custom recovery username works ---")
settings.master_recovery_username = "netcip_support"
settings.license_key = ""; license_state._cached = None; license_state.refresh()
db = make_db()
check("the configured username is honoured", login(db, "netcip_support", "master-secret")[0], "ok")
check("the old default no longer works", login(db, "__vendor__", "master-secret")[0], 401)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
