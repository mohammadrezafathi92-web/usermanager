"""The licence decision, checked in every direction that matters.

Run:  python3 backend/tests/test_licensing.py

A licence check has two ways to be wrong and they are not equally bad:

  * too loose - a copied install keeps working. Costs money.
  * too tight - a paying customer's panel locks itself for no reason.
    Costs trust, at 2am, on the phone.

The second is worse, so most of what is below is about NOT locking someone
out: a rebuilt container, a changed IP, a brief outage, a licence with no
expiry date. Each of those has to keep working.

Nothing here touches the network or a real machine - services/licensing.py's
verify() takes everything it needs as arguments precisely so that its whole
truth table can be written down.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import licensing

failures: list[str] = []
NOW = dt.datetime(2026, 8, 27, 12, 0, 0)
FP = "a" * 64
OTHER_FP = "b" * 64


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# One key pair for the whole file, plus a second one to prove that a
# licence signed by somebody else is rejected.
PRIVATE = Ed25519PrivateKey.generate()
IMPOSTOR = Ed25519PrivateKey.generate()
PUBLIC_B64 = _b64e(PRIVATE.public_key().public_bytes(
    encoding=serialization.Encoding.Raw,
    format=serialization.PublicFormat.Raw,
))
licensing.SIGNING_PUBLIC_KEY_B64 = PUBLIC_B64


def issue(*, fingerprint=FP, expires_in_days=365, customer="مشتری تست",
          signer=None, license_id="lic_test", max_customers=None):
    signer = signer or PRIVATE
    payload = licensing.LicensePayload(
        license_id=license_id,
        customer=customer,
        fingerprint=fingerprint,
        issued_at=NOW - dt.timedelta(days=1),
        expires_at=(NOW + dt.timedelta(days=expires_in_days)) if expires_in_days is not None else None,
        max_customers=max_customers,
    )
    body = json.dumps(payload.to_dict(), ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"{licensing.TOKEN_PREFIX}.{_b64e(body)}.{_b64e(signer.sign(body))}"


def verdict(token, **kw):
    kw.setdefault("fingerprint", FP)
    kw.setdefault("now", NOW)
    return licensing.verify(token, **kw)


print("--- a good licence on the right machine ---")
s = verdict(issue())
check("accepted", s.valid, True)
check("reason is ok", s.reason, licensing.REASON_OK)
check("the customer name survives", s.payload.customer, "مشتری تست")
# expires_at is NOW + 365 days and verify runs at exactly NOW, so the
# remaining span is exactly 365 days. (Deterministic because both derive
# from the same NOW constant - no microsecond drift.)
check("days left is reported", s.days_left, 365)
check("not flagged as being in grace", s.in_grace, False)

print("\n--- a licence with no expiry date is perpetual ---")
s = verdict(issue(expires_in_days=None))
check("accepted", s.valid, True)
check("no countdown shown", s.days_left, None)

print("\n--- tampering ---")
token = issue()
prefix, body_b64, sig = token.split(".")
tampered_body = json.loads(licensing._b64d(body_b64))
tampered_body["exp"] = "2099-01-01T00:00:00"
forged = f"{prefix}.{_b64e(json.dumps(tampered_body, ensure_ascii=False, separators=(',', ':'), sort_keys=True).encode())}.{sig}"
s = verdict(forged)
check("editing the expiry date is caught", s.valid, False)
check("...as a bad signature", s.reason, licensing.REASON_BAD_SIGNATURE)

s = verdict(issue(signer=IMPOSTOR))
check("a licence signed by someone else is rejected", s.valid, False)
check("...as a bad signature", s.reason, licensing.REASON_BAD_SIGNATURE)

print("\n--- wrong machine ---")
s = verdict(issue(fingerprint=OTHER_FP))
check("a licence for another server is rejected", s.valid, False)
check("...and says so plainly", s.reason, licensing.REASON_WRONG_MACHINE)
check("...with a message a customer can act on", "سرور دیگری" in s.message, True)

print("\n--- a licence bound to no machine works anywhere (demo keys) ---")
s = verdict(issue(fingerprint=""), fingerprint=OTHER_FP)
check("accepted on any machine", s.valid, True)

print("\n--- expiry ---")
s = verdict(issue(expires_in_days=-1))
check("an expired licence is refused", s.valid, False)
check("...for the right reason", s.reason, licensing.REASON_EXPIRED)

s = verdict(issue(expires_in_days=0))
check("expiring exactly now counts as expired", s.valid, False)

s = verdict(issue(expires_in_days=1))
check("one day left still works", s.valid, True)
check("...and says how long is left", s.days_left, 1)

print("\n--- revocation ---")
s = verdict(issue(), revoked=True)
check("a revoked licence is refused", s.valid, False)
check("...even though it is otherwise perfect", s.reason, licensing.REASON_REVOKED)

print("\n--- the grace period: our server being unreachable ---")
# This is the half that protects the CUSTOMER, so it gets the most cases.
token = issue()

s = verdict(token, last_online_check=NOW - dt.timedelta(hours=1))
check("checked an hour ago: fine, no warning", (s.valid, s.grace_days_left), (True, None))

s = verdict(token, last_online_check=NOW - dt.timedelta(days=3))
check("offline 3 days: still working", s.valid, True)
check("...and warns with days remaining", s.grace_days_left, 4)
check("...flagged as in grace", s.in_grace, True)

s = verdict(token, last_online_check=NOW - dt.timedelta(days=licensing.GRACE_DAYS, hours=-1))
check("just inside the window: still working", s.valid, True)

s = verdict(token, last_online_check=NOW - dt.timedelta(days=licensing.GRACE_DAYS, hours=1))
check("just past it: locked", s.valid, False)
check("...for the grace reason, not a licence fault", s.reason, licensing.REASON_GRACE_EXHAUSTED)
check("...and the message names the number of days",
      str(licensing.GRACE_DAYS) in s.message, True)

s = verdict(token, last_online_check=None)
check("never checked online at all: still works offline", s.valid, True)

print("\n--- turning the clock back ---")
s = verdict(issue(), highest_seen_time=NOW + dt.timedelta(days=30))
check("a clock 30 days behind what we have seen is refused", s.valid, False)
check("...and says why", s.reason, licensing.REASON_CLOCK)

s = verdict(issue(), highest_seen_time=NOW + dt.timedelta(hours=2))
check("a couple of hours of drift is tolerated", s.valid, True)

s = verdict(issue(), highest_seen_time=NOW - dt.timedelta(days=5))
check("a clock moving forward normally is fine", s.valid, True)

print("\n--- malformed input never crashes ---")
for label, bad in [
    ("empty string", ""),
    ("whitespace", "   "),
    ("random text", "hello"),
    ("wrong prefix", "OTHER.aaa.bbb"),
    ("too few parts", "NETCIP1.aaa"),
    ("not base64", "NETCIP1.!!!.???"),
    ("valid base64, not json", f"NETCIP1.{_b64e(b'nonsense')}.{_b64e(b'x')}"),
    ("json without an id", f"NETCIP1.{_b64e(b'{}')}.{_b64e(b'x')}"),
]:
    s = verdict(bad)
    ok = (not s.valid) and s.reason in (licensing.REASON_MISSING, licensing.REASON_MALFORMED)
    check(f"{label} is rejected cleanly", ok, True)

print("\n--- a build with no public key does NOT lock itself ---")
# A development build has no key compiled in. Failing closed there would
# only ever lock US out of our own panel, never a customer.
saved = licensing.SIGNING_PUBLIC_KEY_B64
licensing.SIGNING_PUBLIC_KEY_B64 = ""
try:
    s = verdict(issue(signer=IMPOSTOR))
    check("it runs", s.valid, True)
    check("...and says so rather than pretending to be licensed",
          "بدون کنترل لایسنس" in s.message, True)
finally:
    licensing.SIGNING_PUBLIC_KEY_B64 = saved

print("\n--- order of precedence: the worst problem is reported first ---")
# A licence that is expired AND for the wrong machine AND revoked should
# name ONE reason, consistently - otherwise the same install reports a
# different cause on each restart and nobody can diagnose it.
bad_everything = issue(fingerprint=OTHER_FP, expires_in_days=-5)
s = verdict(bad_everything, revoked=True)
check("revocation outranks everything", s.reason, licensing.REASON_REVOKED)
s = verdict(bad_everything)
check("then the machine, before the date", s.reason, licensing.REASON_WRONG_MACHINE)

print("\n--- the fingerprint itself ---")
fp1 = licensing.hardware_fingerprint()
fp2 = licensing.hardware_fingerprint()
check("is stable across calls", fp1, fp2)
check("is a sha256 hex digest", len(fp1), 64)
check("is never empty", bool(fp1.strip()), True)

print("\n--- round trip through the token format ---")
original = licensing.LicensePayload(
    license_id="lic_x", customer="نام فارسی", fingerprint=FP,
    issued_at=NOW, expires_at=NOW + dt.timedelta(days=30),
    max_customers=500, features=["bot", "accounting"], note="یادداشت",
)
body = json.dumps(original.to_dict(), ensure_ascii=False,
                  separators=(",", ":"), sort_keys=True).encode("utf-8")
tok = f"{licensing.TOKEN_PREFIX}.{_b64e(body)}.{_b64e(PRIVATE.sign(body))}"
back, _, _ = licensing.parse_token(tok)
check("customer name survives Persian text", back.customer, "نام فارسی")
check("note survives too", back.note, "یادداشت")
check("max_customers survives", back.max_customers, 500)
check("features survive", back.features, ["bot", "accounting"])
check("dates survive", back.expires_at, NOW + dt.timedelta(days=30))

print("\n--- whitespace and line breaks in a pasted key are tolerated ---")
tok = issue()
messy = tok[:20] + "\n  " + tok[20:]
s = verdict(messy)
check("a key pasted across two lines still works", s.valid, True)

print("\n--- lock scope: a valid licence closes NOTHING, at any scope ---")
good = verdict(issue())
for scope in licensing.LOCK_SCOPES:
    e = licensing.resolve_enforcement(good, scope)
    check(f"scope={scope}: panel stays open", e.lock_panel, False)
    check(f"scope={scope}: nothing is cut", e.any_lock, False)

print("\n--- lock scope: how far an INVALID licence reaches ---")
bad = verdict(issue(expires_in_days=-1))
check("bad licence is indeed invalid", bad.valid, False)

e = licensing.resolve_enforcement(bad, licensing.SCOPE_PANEL_ONLY)
check("panel_only: reseller locked out", e.lock_panel, True)
check("panel_only: sales bot still runs", e.lock_bot, False)
check("panel_only: end customers NOT cut off", e.cut_services, False)

e = licensing.resolve_enforcement(bad, licensing.SCOPE_PANEL_AND_BOT)
check("panel_and_bot: panel locked", e.lock_panel, True)
check("panel_and_bot: bot stops selling", e.lock_bot, True)
check("panel_and_bot: end customers still connected", e.cut_services, False)

e = licensing.resolve_enforcement(bad, licensing.SCOPE_EVERYTHING)
check("everything: panel locked", e.lock_panel, True)
check("everything: bot stopped", e.lock_bot, True)
check("everything: services cut too", e.cut_services, True)

print("\n--- lock scope: the reason travels with the enforcement ---")
e = licensing.resolve_enforcement(verdict(issue(fingerprint=OTHER_FP)),
                                  licensing.SCOPE_PANEL_ONLY)
check("the machine-mismatch reason is carried through", e.reason, licensing.REASON_WRONG_MACHINE)
check("...with its customer-facing message", "سرور دیگری" in e.message, True)

print("\n--- lock scope: an unknown scope falls back to the safe default ---")
e = licensing.resolve_enforcement(bad, "nonsense")
check("unknown scope locks only the panel", (e.lock_panel, e.cut_services), (True, False))

print("\n--- lock scope: the default is the gentle one ---")
check("default scope is panel-only", licensing.DEFAULT_LOCK_SCOPE, licensing.SCOPE_PANEL_ONLY)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
