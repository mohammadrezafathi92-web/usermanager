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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Point the app at a private in-memory DB and give it a known password
# BEFORE importing it, since it wires the engine at import time.
os.environ["LICENSE_DB_URL"] = "sqlite://"
os.environ["LICENSE_ADMIN_PASSWORD"] = "test-pass"

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
check("exactly two fields", sorted(r.json().keys()), ["lock_scope", "revoked"])

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

print("\n--- logout drops the session ---")
client.get("/console/logout")
r = client.get("/console")
check("after logout the console is locked again", "رمز عبور" in r.text, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
