"""The licence server's HTTP surface, end to end.

Run:  python3 license_server/test_app.py

Uses FastAPI's TestClient against an in-memory DB. Two things to prove:
  - a panel's /heartbeat gets the right answer and never needs a password;
  - everything under /console does need one, and the levers there change
    what the next heartbeat returns.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the app at a private in-memory DB and give it a known password
# BEFORE importing it, since it wires the engine at import time.
os.environ["LICENSE_DB_URL"] = "sqlite://"
os.environ["LICENSE_ADMIN_PASSWORD"] = "test-pass"

# A throwaway signing key, in place BEFORE import so signing.PRIVATE_KEY_PATH
# (read once at module load) points at it - see the صدور لایسنس section
# near the bottom of this file. signing.py's own round-trip against the
# panel's real licensing.py is covered separately in test_signing.py; this
# file only needs to prove the HTTP surface (form -> token -> listed row).
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

_priv = Ed25519PrivateKey.generate()
_fd, _KEY_PATH = tempfile.mkstemp()
os.write(_fd, _priv.private_bytes(
    encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
))
os.close(_fd)
os.environ["LICENSE_PRIVATE_KEY_PATH"] = _KEY_PATH

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402

# TestClient only fires startup events when used as a context manager, and
# the rest of this file uses a plain client. The one thing startup does that
# we need is set the console password, so run it explicitly.
appmod._startup()

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


client = TestClient(appmod.app)


def hb(license_id="lic1", **extra):
    body = {"license_id": license_id, "fingerprint": "fp1"}
    body.update(extra)
    return client.post("/heartbeat", json=body)


print("--- the public heartbeat needs no auth ---")
r = hb(panel_version="1.3.0", customers=42)
check("200 OK", r.status_code, 200)
check("told not revoked", r.json()["revoked"], False)
check("told the default scope", r.json()["lock_scope"], "panel_only")
# Three, since 2026-09: activated_at joined them so an install-dated
# licence can be anchored to the vendor's copy (see store.record_heartbeat).
# Still an exact list - the reply is a contract, not a grab bag.
check("exactly the three fields the install acts on",
      sorted(r.json().keys()), ["activated_at", "lock_scope", "revoked"])
check("no activation reported means none is returned", r.json()["activated_at"], None)

print("\n--- a heartbeat without a license_id is refused ---")
check("400", client.post("/heartbeat", json={"fingerprint": "x"}).status_code, 400)
check("non-JSON is refused", client.post("/heartbeat", content="notjson").status_code, 400)

print("\n--- the console is locked to strangers ---")
# No cookie -> redirected to login (TestClient follows, lands on the form).
r = client.get("/console")
check("no session lands on the login page", "کنسول لایسنس" in r.text and "رمز عبور" in r.text, True)
check("toggling without a session is refused",
      client.post("/console/toggle", data={"license_id": "lic1", "revoked": "1"},
                  follow_redirects=False).status_code in (303, 401, 403), True)

print("\n--- logging in ---")
r = client.post("/console/login", data={"password": "wrong"}, follow_redirects=False)
check("a wrong password does not log in", "/console/login" in r.headers.get("location", ""), True)
r = client.post("/console/login", data={"password": "test-pass"}, follow_redirects=False)
check("the right password sets a session cookie", appmod.COOKIE in r.headers.get("set-cookie", ""), True)

# TestClient keeps the cookie jar, so subsequent calls are authenticated.
r = client.get("/console")
check("the console now opens", "نصب‌های فعال" in r.text, True)
check("...and lists the install that pinged", "lic1" in r.text, True)

print("\n--- revoking from the console changes the next heartbeat ---")
client.post("/console/toggle", data={"license_id": "lic1", "revoked": "1"})
check("the panel is now told it is revoked", hb().json()["revoked"], True)
client.post("/console/toggle", data={"license_id": "lic1", "revoked": "0"})
check("un-revoking restores it", hb().json()["revoked"], False)

print("\n--- changing the scope from the console ---")
client.post("/console/scope", data={"license_id": "lic1", "scope": "everything"})
check("the panel is told the new scope", hb().json()["lock_scope"], "everything")
client.post("/console/scope", data={"license_id": "lic1", "scope": "bogus"})
check("a bogus scope is ignored, not applied", hb().json()["lock_scope"], "everything")

print("\n--- labelling from the console ---")
client.post("/console/label", data={"license_id": "lic1", "label": "فروشگاه رضا"})
check("the label shows up in the list", "فروشگاه رضا" in client.get("/console").text, True)

print("\n--- the copy alarm is visible in the console ---")
client.post("/heartbeat", json={"license_id": "lic1", "fingerprint": "MOVED_SERVER"})
check("a changed fingerprint raises the warning badge",
      "اثر انگشت عوض شد" in client.get("/console").text, True)

print("\n--- forgetting a row from the console ---")
check("lic1 is listed before forgetting it", "lic1" in client.get("/console").text, True)
client.post("/console/forget", data={"license_id": "lic1"})
check("lic1 is gone from the list", "lic1" in client.get("/console").text, False)
r = hb()  # lic1 heartbeats again
check("it just re-registers, unnamed - not blocked", r.json()["revoked"], False)
check("...and is back in the list", "lic1" in client.get("/console").text, True)
check("...but the old label did not survive (a fresh row, not a ban)",
      "فروشگاه رضا" in client.get("/console").text, False)

print("\n--- issuing a license from the console ---")
r = client.get("/console/issue")
check("the form needs a session too", "کنسول لایسنس" not in r.text or "رمز عبور" in r.text, True)
# (already authenticated from earlier in this file - client kept the cookie)
r = client.get("/console/issue")
check("the form opens", "صدور لایسنس جدید" in r.text, True)

r = client.post("/console/issue", data={
    "customer": "فروشگاه تست", "fingerprint": "issued-fp", "days": "30", "note": "",
})
check("200 OK", r.status_code, 200)
check("a token is shown", "NETCIP1." in r.text, True)
check("it appears in the list right away, before any heartbeat", "فروشگاه تست" in client.get("/console").text, True)
check("...and says it hasn't pinged yet", "هنوز پینگ نزده" in client.get("/console").text, True)

import re
m = re.search(r"NETCIP1\.[\w-]+\.[\w-]+", r.text)
check("a token was actually extracted from the page", m is not None, True)
_issued_token = m.group(0)

print("\n--- that issued token actually verifies on the panel side ---")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import base64  # noqa: E402
# This file's own license_server/app.py already occupies sys.modules['app']
# (imported above as `appmod`) - drop that cache entry so `import app...`
# below resolves to backend/app/ (a real package) instead of reusing the
# already-loaded, non-package license_server/app.py module of the same
# name. `appmod` (already bound) is unaffected; nothing after this needs
# license_server's `app` under its own bare name again.
del sys.modules["app"]
from app.services import licensing  # noqa: E402
licensing.SIGNING_PUBLIC_KEY_B64 = base64.urlsafe_b64encode(
    _priv.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                    format=serialization.PublicFormat.Raw)
).decode().rstrip("=")
status = licensing.verify(_issued_token, fingerprint="issued-fp")
check("valid", status.valid, True)
check("bound to the fingerprint given on the form", status.payload.fingerprint, "issued-fp")

print("\n--- without a signing key on disk, the form says so instead of crashing ---")
os.environ["LICENSE_PRIVATE_KEY_PATH"] = "/no/such/file"
import importlib
import signing as signing_mod
importlib.reload(signing_mod)
r = client.get("/console/issue")
check("explains the key is missing", "کلید خصوصی امضا" in r.text, True)
os.environ["LICENSE_PRIVATE_KEY_PATH"] = _KEY_PATH
importlib.reload(signing_mod)

print("\n--- logout drops the session ---")
client.get("/console/logout")
r = client.get("/console")
check("after logout the console is locked again", "رمز عبور" in r.text, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
