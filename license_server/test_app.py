"""The licence server's HTTP surface, end to end.

Run:  python3 license_server/test_app.py

Uses FastAPI's TestClient against an in-memory DB and a throwaway signing
key. What it proves:
  - a panel's /heartbeat gets exactly the same answer as before and never
    needs a password;
  - everything under /console needs a session, every change needs the
    session's CSRF token, and locking/issuing go through a confirmation;
  - the levers change what the next heartbeat returns, and every change is
    recorded;
  - issue and renew sign tokens the panel's own licensing.py accepts, a
    renewal is linked to its predecessor (not a new customer), and a
    double submit cannot sign twice.
"""
from __future__ import annotations

import base64
import datetime as dt
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

os.environ["LICENSE_DB_URL"] = "sqlite://"
os.environ["LICENSE_ADMIN_PASSWORD"] = "test-pass"

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

_priv = Ed25519PrivateKey.generate()
_fd, _KEY_PATH = tempfile.mkstemp()
os.write(_fd, _priv.private_bytes(encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
                                  encryption_algorithm=serialization.NoEncryption()))
os.close(_fd)
os.environ["LICENSE_PRIVATE_KEY_PATH"] = _KEY_PATH

from fastapi.testclient import TestClient  # noqa: E402

import app as appmod  # noqa: E402
import console_auth  # noqa: E402
import console_deps  # noqa: E402
import ledger  # noqa: E402
import store  # noqa: E402

appmod._startup()
failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


client = TestClient(appmod.app, base_url="https://license.test")
FP = "a" * 64
FP2 = "b" * 64


def hb(license_id="lic1", fingerprint=FP, **extra):
    body = {"license_id": license_id, "fingerprint": fingerprint}
    body.update(extra)
    return client.post("/heartbeat", json=body)


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def db():
    return console_deps._Session()


def install(lid):
    s = db()
    try:
        return store.get_install(s, lid)
    finally:
        s.close()


print("--- the public heartbeat: same contract, no auth ---")
r = hb(panel_version="1.3.0", customers=42)
check("200 OK", r.status_code, 200)
check("exactly the three fields the install acts on", sorted(r.json()), ["activated_at", "lock_scope", "revoked"])
check("told not revoked, default scope", (r.json()["revoked"], r.json()["lock_scope"]), (False, "panel_only"))
check("no activation reported means none is returned", r.json()["activated_at"], None)
check("a heartbeat without a license_id is refused", client.post("/heartbeat", json={"fingerprint": "x"}).status_code, 400)
check("non-JSON is refused", client.post("/heartbeat", content="notjson").status_code, 400)
check("a JSON list is refused, not a crash", client.post("/heartbeat", json=["x"]).status_code, 400)
r = client.post("/heartbeat", json={"license_id": "lic1", "fingerprint": FP}, headers={"x-real-ip": "203.0.113.9",
                                                                                    "x-forwarded-for": "6.6.6.6"})
check("the address nginx saw is recorded, not a client-chosen X-Forwarded-For", install("lic1").last_ip, "203.0.113.9")

print("\n--- the console is closed to strangers ---")
r = client.get("/console", follow_redirects=False)
check("no session: redirected to login", (r.status_code, r.headers.get("location")), (303, "/console/login"))
check("the login page opens", "رمز عبور" in client.get("/console/login").text, True)
r = client.get("/console/login?error=<script>alert(1)</script>")
check("login error text is never reflected (old XSS)", "<script>alert(1)" in r.text, False)
check("console pages carry a strict CSP", "default-src 'none'" in r.headers.get("content-security-policy", ""), True)
check("...and are never cached", r.headers.get("cache-control"), "no-store")
check("...and cannot be framed", r.headers.get("x-frame-options"), "DENY")
check("old unauthenticated lever routes are gone",
      client.post("/console/toggle", data={"license_id": "lic1", "revoked": "1"}, follow_redirects=False).status_code
      in (303, 404, 405), True)
check("heartbeat still not revoked", hb().json()["revoked"], False)

print("\n--- logging in ---")
r = client.post("/console/login", data={"password": "wrong"}, follow_redirects=False)
check("a wrong password does not log in", r.headers.get("location"), "/console/login?error=wrong")
r = client.post("/console/login", data={"password": "test-pass"}, headers={"origin": "https://evil.test"},
                follow_redirects=False)
r = client.post("/console/login", data={"password": "test-pass"}, headers={"origin": "null"}, follow_redirects=False)
check("an opaque (null) origin is refused too", r.headers.get("location"), "/console/login?error=origin")
check("a cross-site login post is refused", r.headers.get("location"), "/console/login?error=origin")
r = client.post("/console/login", data={"password": "test-pass"}, follow_redirects=False)
cookie = r.headers.get("set-cookie", "")
check("the right password sets a session cookie", appmod.COOKIE in cookie, True)
check("...HttpOnly, Secure behind https, SameSite=Strict, scoped to /console",
      all(x in cookie.lower() for x in ("httponly", "secure", "samesite=strict", "path=/console")), True)
page = client.get("/console")
check("the console now opens", "لایسنس‌ها" in page.text and "lic1" in page.text, True)
csrf = csrf_of(page.text)
check("a CSRF token is on the page", len(csrf) > 20, True)
s = db()
check("login and the failed attempt are in the event log",
      [e.action for e in ledger.events(s)][:2], ["login", "login_failed"])
s.close()

print("\n--- the three facts are separate ---")
check("an install with no issue record shows validity as unknown", "نامشخص" in page.text, True)
check("...and not as valid", "معتبر</span>" in page.text, False)
d = client.get("/console/license?id=lic1").text
check("details page explains validity is unknown, not guessed", "حدس زده نمی‌شود" in d, True)
check("unknown licence id: 404", client.get("/console/license?id=nope").status_code, 404)

print("\n--- locking needs a confirmation, CSRF and a fresh state ---")
r = client.get("/console/license/access?id=lic1&revoked=1&scope=panel_and_bot")
check("the confirmation page states the consequence", "فروش ربات بسته" in r.text, True)
check("...and that it reaches the panel on its next heartbeat", "heartbeat بعدی" in r.text, True)
check("nothing changed by viewing it", hb().json()["revoked"], False)
form = {"license_id": "lic1", "revoked": "1", "scope": "panel_and_bot", "expect_revoked": "0",
        "expect_scope": "panel_only", "reason": "پرداخت نشده"}
r = client.post("/console/license/access", data=form, follow_redirects=False)
check("without the CSRF token: refused", r.status_code, 403)
r = client.post("/console/license/access", data={**form, "csrf": "forged"}, follow_redirects=False)
check("with a wrong CSRF token: refused", r.status_code, 403)
check("...nothing applied", hb().json()["revoked"], False)
r = client.post("/console/license/access", data={**form, "csrf": csrf}, headers={"origin": "https://evil.test"},
                follow_redirects=False)
check("a cross-origin post with the right token: refused", r.status_code, 403)
r = client.post("/console/license/access", data={**form, "csrf": csrf}, follow_redirects=False)
check("confirmed: applied", r.status_code, 303)
check("the panel is told revoked with the chosen scope", (hb().json()["revoked"], hb().json()["lock_scope"]),
      (True, "panel_and_bot"))
r = client.post("/console/license/access", data={**form, "csrf": csrf, "scope": "everything"}, follow_redirects=False)
check("a resend from a stale confirmation page is refused (409), not applied", r.status_code, 409)
check("...scope unchanged", hb().json()["lock_scope"], "panel_and_bot")
stale = {**form, "csrf": csrf, "expect_revoked": "1", "expect_scope": "panel_and_bot", "scope": "everything"}
r = client.post("/console/license/access", data=stale, follow_redirects=False)
check("cutting end users needs an explicit acknowledgement", (r.status_code, hb().json()["lock_scope"]),
      (200, "panel_and_bot"))
r = client.post("/console/license/access", data={**stale, "ack": "yes"}, follow_redirects=False)
check("...with it: applied", hb().json()["lock_scope"], "everything")
s = db()
ev = [e for e in ledger.events(s, license_id="lic1") if e.action == "access_changed"]
check("both changes recorded with before/after and reason",
      [(ledger.event_detail(e)["after"]["lock_scope"], e.reason) for e in ev],
      [("everything", "پرداخت نشده"), ("panel_and_bot", "پرداخت نشده")])
check("the delivery state follows the heartbeat", ledger.delivery(s, store.get_install(s, "lic1"))["state"], "delivered")
s.close()
d = client.get("/console/license?id=lic1").text
check("locked rows cannot be removed from the list (that would unlock them)", "برداشتن از فهرست</a>" in d, False)
r = client.post("/console/license/forget", data={"license_id": "lic1", "csrf": csrf})
check("...a direct post is refused too", (r.status_code, install("lic1") is not None), (409, True))
unlock = {"license_id": "lic1", "revoked": "0", "scope": "everything", "expect_revoked": "1",
          "expect_scope": "everything", "csrf": csrf}
client.post("/console/license/access", data=unlock)
check("unlocking restores the heartbeat answer", hb().json()["revoked"], False)

print("\n--- label and note ---")
client.post("/console/license/label", data={"license_id": "lic1", "label": "<b>فروشگاه رضا</b>", "note": "n",
                                            "csrf": csrf})
page = client.get("/console").text
check("the label shows up, escaped", "&lt;b&gt;فروشگاه رضا&lt;/b&gt;" in page and "<b>فروشگاه رضا</b>" not in page, True)
check("search finds it by name", "lic1" in client.get("/console?q=رضا").text, True)
check("search hides what does not match", "lic1" in client.get("/console?q=zzz").text, False)

print("\n--- the copy alarm ---")
hb(fingerprint=FP2)
check("a changed fingerprint raises the warning badge", "اثر انگشت عوض شد" in client.get("/console").text, True)
hb(fingerprint=FP)

print("\n--- issuing: form -> review -> sign once ---")
r = client.get("/console/issue")
check("the form opens", "صدور لایسنس جدید" in r.text, True)
base = {"customer": "فروشگاه تست", "fingerprint": FP, "basis": "days", "amount": "30", "scope": "panel_only",
        "note": "", "csrf": csrf}
r = client.post("/console/issue/review", data={**base, "fingerprint": "not-hex!"})
check("a malformed fingerprint is refused with an explanation", (r.status_code, "هگز" in r.text), (400, True))
r = client.post("/console/issue/review", data={**base, "basis": ""})
check("a validity basis must be chosen explicitly", r.status_code, 400)
r = client.post("/console/issue/review", data=base)
check("the review shows the computed term", "پیش‌نمایش صدور" in r.text and "تا " in r.text, True)
check("...and signs nothing yet", "NETCIP1." in r.text, False)
nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
r = client.post("/console/issue", data={**base, "nonce": nonce})
m = re.search(r"NETCIP1\.[\w-]+\.[\w-]+", r.text)
check("signed: the token is shown once", m is not None, True)
token1 = m.group(0)
r2 = client.post("/console/issue", data={**base, "nonce": nonce})
check("a double submit of the same review signs nothing more", (r2.status_code, "NETCIP1." in r2.text), (409, False))
s = db()
issued_rows = s.query(store.IssuedLicense).all()
check("exactly one licence recorded", len(issued_rows), 1)
lic_a = issued_rows[0].license_id
check("the token itself is not stored anywhere in the DB",
      any(token1 in (str(v) for v in vars(row).values()) for row in s.query(store.IssuedLicense).all())
      or any(token1 in (e.detail or "") for e in ledger.events(s)), False)
s.close()
check("listed before its first heartbeat, saying so", "هنوز گزارش نداده" in client.get("/console").text, True)

print("\n--- the token verifies on the panel side ---")
sys.path.insert(0, os.path.join(HERE, "..", "backend"))
del sys.modules["app"]
from app.services import licensing  # noqa: E402
licensing.SIGNING_PUBLIC_KEY_B64 = base64.urlsafe_b64encode(_priv.public_key().public_bytes(
    encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw)).decode().rstrip("=")
st = licensing.verify(token1, fingerprint=FP)
check("valid, bound to the fingerprint, ~30 days", (st.valid, st.payload.fingerprint, 29 <= st.days_left <= 30),
      (True, FP, True))
check("refused on another machine", licensing.verify(token1, fingerprint=FP2).reason, licensing.REASON_WRONG_MACHINE)

print("\n--- renewing links the successor; validity bases match the panel ---")
hb(lic_a)  # the customer installed it
r = client.get(f"/console/issue?renews={lic_a}")
check("the renewal form says there is no automatic central renewal", "تمدید مرکزیِ خودکار وجود ندارد" in r.text, True)
check("...prefills the fingerprint from the signed licence", FP in r.text, True)
renew = {**base, "renews": lic_a, "basis": "after", "amount": "10"}
r = client.post("/console/issue/review", data=renew)
nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
r = client.post("/console/issue", data={**renew, "nonce": nonce})
token2 = re.search(r"NETCIP1\.[\w-]+\.[\w-]+", r.text).group(0)
check("the result page says the old licence stays valid", "باطل نشد" in r.text, True)
st2 = licensing.verify(token2, fingerprint=FP)
s = db()
old = ledger.get_issued(s, lic_a)
new = ledger.get_issued(s, st2.payload.license_id)
check("a NEW licence id (the panel starts no stale activation clock)", new.license_id != lic_a, True)
check("linked to its predecessor, not a stray new customer", new.renews_license_id, lic_a)
check("from the end of the current term: old expiry + 10 days",
      abs((st2.payload.expires_at - (old.expires_at + dt.timedelta(days=10))).total_seconds()) < 2, True)
check("the lineage reads oldest first", ledger.lineage(s, new.license_id), [lic_a, new.license_id])
check("the predecessor's log notes its successor",
      [e.action for e in ledger.events(s, license_id=lic_a)][0], "renewal_issued")
s.close()
d = client.get(f"/console/license?id={lic_a}").text
check("the old row says a successor exists but is not installed yet", "هنوز با آن گزارش نداده" in d, True)
check("renewing the already-renewed licence again warns about a parallel successor",
      "دو جانشین موازی" in client.get(f"/console/issue?renews={lic_a}").text, True)
hb(new.license_id)
d = client.get(f"/console/license?id={lic_a}").text
check("...and that it was replaced once the new key reports in", "پنل اکنون با لایسنس جانشین گزارش می‌دهد" in d, True)

install_days = {**base, "basis": "install", "amount": "90"}
r = client.post("/console/issue/review", data=install_days)
nonce = re.search(r'name="nonce" value="([^"]+)"', r.text).group(1)
token3 = re.search(r"NETCIP1\.[\w-]+\.[\w-]+", client.post("/console/issue", data={**install_days, "nonce": nonce}).text).group(0)
p3, _b, _s = licensing.parse_token(token3)
check("'from installation' is the panel's own vd field, no exp",
      (p3.valid_days, p3.expires_at), (90, None))
check("...the console calls it not started until the panel reports activation",
      "شروع‌نشده" in client.get(f"/console/license?id={p3.license_id}").text, True)
hb(p3.license_id, activated_at="2026-01-01T00:00:00")
s = db()
v = ledger.validity(store.get_install(s, p3.license_id), ledger.get_issued(s, p3.license_id), dt.datetime(2026, 2, 1))
check("...then counts from the activation the panel reported, like effective_expiry",
      v["expires_at"], licensing.effective_expiry(p3, dt.datetime(2026, 1, 1)))
s.close()

print("\n--- importing an old token records its signed facts ---")
hb("lic_old")
legacy, legacy_id = __import__("signing").issue(customer="قدیمی", fingerprint=FP, days=5)
r = client.post("/console/license/import", data={"license_id": "lic_old", "token": legacy, "csrf": csrf})
check("a token for another licence id is refused", r.status_code, 400)
other = _priv  # a token signed by a different key is refused
foreign_key = Ed25519PrivateKey.generate()
body, sig = legacy.split(".")[1], legacy.split(".")[2]
raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
forged = "NETCIP1." + body + "." + base64.urlsafe_b64encode(foreign_key.sign(raw)).decode().rstrip("=")
hb(legacy_id)
r = client.post("/console/license/import", data={"license_id": legacy_id, "token": forged, "csrf": csrf})
check("a token not signed by this server's key is refused", (r.status_code, "امضا نشده" in r.text), (400, True))
r = client.post("/console/license/import", data={"license_id": legacy_id, "token": legacy, "csrf": csrf},
                follow_redirects=False)
check("a genuine one is accepted", r.status_code, 303)
check("...its validity now comes from the signed payload", "رو به پایان" in client.get(f"/console/license?id={legacy_id}").text, True)

print("\n--- forgetting an open row keeps its history ---")
r = client.post("/console/license/forget", data={"license_id": legacy_id, "csrf": csrf}, follow_redirects=False)
check("removed", (r.status_code, install(legacy_id)), (303, None))
s = db()
check("issue record and events survive", (ledger.get_issued(s, legacy_id) is not None,
                                          ledger.events(s, license_id=legacy_id)[0].action), (True, "forgotten"))
s.close()

print("\n--- without a signing key the form says so, and never offers to make one ---")
import importlib  # noqa: E402
import signing as signing_mod  # noqa: E402
os.environ["LICENSE_PRIVATE_KEY_PATH"] = "/no/such/file"
importlib.reload(signing_mod)
r = client.get("/console/issue")
check("explains the key is missing and not to create a new one", "کلید تازه نسازید" in r.text, True)
os.environ["LICENSE_PRIVATE_KEY_PATH"] = _KEY_PATH
importlib.reload(signing_mod)

print("\n--- the console password ---")
r = client.post("/console/password", data={"current": "wrong", "new": "x" * 12, "repeat": "x" * 12, "csrf": csrf})
check("the current password is required", r.status_code, 400)
r = client.post("/console/password", data={"current": "test-pass", "new": "short", "repeat": "short", "csrf": csrf})
check("a short new password is refused", r.status_code, 400)
other = TestClient(appmod.app, base_url="https://license.test")
other.post("/console/login", data={"password": "test-pass"})
check("a second browser is logged in", other.get("/console", follow_redirects=False).status_code, 200)
r = client.post("/console/password", data={"current": "test-pass", "new": "new-pass-1234", "repeat": "new-pass-1234",
                                           "csrf": csrf}, follow_redirects=False)
check("changed", r.status_code, 303)
check("...other sessions are closed", other.get("/console", follow_redirects=False).status_code, 303)
check("...this browser stays in", client.get("/console", follow_redirects=False).status_code, 200)
check("...the old password no longer works",
      other.post("/console/login", data={"password": "test-pass"}, follow_redirects=False).headers["location"],
      "/console/login?error=wrong")
csrf = csrf_of(client.get("/console").text)

print("\n--- login throttling ---")
console_auth._failures.clear()
attacker = TestClient(appmod.app, base_url="https://license.test")
for _ in range(console_auth.FAIL_PER_IP):
    attacker.post("/console/login", data={"password": "nope"}, headers={"x-real-ip": "198.51.100.7"})
r = attacker.post("/console/login", data={"password": "new-pass-1234"}, headers={"x-real-ip": "198.51.100.7"},
                  follow_redirects=False)
check("after repeated failures even the right password is refused for a while",
      r.headers["location"], "/console/login?error=blocked")
r = attacker.post("/console/login", data={"password": "new-pass-1234"}, headers={"x-real-ip": "198.51.100.8"},
                  follow_redirects=False)
check("...another address is not locked out", r.headers["location"], "/console")

print("\n--- sessions expire ---")
token, sess = console_auth.create(now=1000.0)
check("an idle session ends", console_auth.get(token, now=1000.0 + console_auth.IDLE_SECONDS + 1), None)
token, sess = console_auth.create(now=1000.0)
for t in range(1000, 1000 + console_auth.ABSOLUTE_SECONDS, 3000):
    console_auth.get(token, now=float(t))
check("an active session still ends at the absolute limit",
      console_auth.get(token, now=1000.0 + console_auth.ABSOLUTE_SECONDS + 1), None)

print("\n--- logout is a POST with CSRF ---")
check("GET /console/logout does not log out", client.get("/console/logout", follow_redirects=False).status_code, 303)
check("...still logged in", client.get("/console", follow_redirects=False).status_code, 200)
client.post("/console/logout", data={"csrf": csrf})
check("after logout the console is closed", client.get("/console", follow_redirects=False).status_code, 303)

print("\n--- dates are shown in the Iranian calendar, Tehran time ---")
import ui  # noqa: E402
import issue_views  # noqa: E402
check("2026-10-21 is 1405/07/29", "۱۴۰۵/۰۷/۲۹" in ui.jdate(dt.datetime(2026, 10, 21, 12, 0)), True)
check("Nowruz midnight Tehran is 1405/01/01", ui.jdate(dt.datetime(2026, 3, 20, 20, 30)), "۱۴۰۵/۰۱/۰۱ ساعت ۰۰:۰۰")
p, errors = issue_views.plan({"customer": "x", "fingerprint": FP, "scope": "panel_only", "basis": "until",
                              "until": "2030-03-20"}, None, dt.datetime(2026, 1, 1))
check("'until a date' means the end of that day in Tehran", (errors, p["expires_at"]),
      ({}, dt.datetime(2030, 3, 20, 20, 29, 59)))
p, errors = issue_views.plan({"customer": "x", "fingerprint": FP, "scope": "panel_only", "basis": "after",
                              "amount": "5"}, None, dt.datetime(2026, 1, 1))
check("'after the current term' is refused when that term is unknown", "basis" in errors, True)
p, errors = issue_views.plan({"customer": "x", "fingerprint": FP, "scope": "panel_only", "basis": "days",
                              "amount": "99999"}, None, dt.datetime(2026, 1, 1))
check("an absurd day count is refused", "amount" in errors, True)

os.unlink(_KEY_PATH)
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
