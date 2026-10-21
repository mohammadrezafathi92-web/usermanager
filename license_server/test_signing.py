"""signing.py - the console's صدور لایسنس page must produce a token the
panel's own backend/app/services/licensing.py actually accepts.

Run:  python3 license_server/test_signing.py

This is the one place the two independent implementations (this service's
signing.py and the panel's licensing.py) are checked against each other -
everywhere else they are deliberately kept apart (see store.py's module
docstring). If they ever drift, THIS is where it would show up: a token
issued from the console that the panel refuses.
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# license_tool.py / licensing.py live under backend/ - reach them the same
# way license_tool.py itself does, without making this service depend on
# the panel package at import time for anything other than this test.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# A throwaway key pair, private key written to a temp file the way it would
# really sit on the license-server's /data volume.
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import base64

_priv = Ed25519PrivateKey.generate()
_raw = _priv.private_bytes(
    encoding=serialization.Encoding.Raw, format=serialization.PrivateFormat.Raw,
    encryption_algorithm=serialization.NoEncryption(),
)
_fd, _key_path = tempfile.mkstemp()
os.write(_fd, _raw)
os.close(_fd)
os.environ["LICENSE_PRIVATE_KEY_PATH"] = _key_path

import signing  # noqa: E402 - after LICENSE_PRIVATE_KEY_PATH is set

pub_b64 = base64.urlsafe_b64encode(
    _priv.public_key().public_bytes(encoding=serialization.Encoding.Raw,
                                    format=serialization.PublicFormat.Raw)
).decode().rstrip("=")

from app.services import licensing  # noqa: E402
licensing.SIGNING_PUBLIC_KEY_B64 = pub_b64


print("--- has_signing_key reflects whether the file is actually there ---")
check("the key file we just wrote is seen", signing.has_signing_key(), True)

print("\n--- a token issued here verifies clean on the panel side ---")
token, license_id = signing.issue(customer="فروشگاه رضا", fingerprint="abc123", days=365)
status = licensing.verify(token, fingerprint="abc123")
check("valid", status.valid, True)
check("reason is ok", status.reason, licensing.REASON_OK)
check("the license_id round-trips", status.payload.license_id, license_id)
check("the customer name round-trips", status.payload.customer, "فروشگاه رضا")
check("days_left is close to 365", 363 <= status.days_left <= 365, True)

print("\n--- wrong machine is still caught, same as a CLI-issued token ---")
status = licensing.verify(token, fingerprint="someone-elses-machine")
check("rejected", status.valid, False)
check("reason is wrong_machine", status.reason, licensing.REASON_WRONG_MACHINE)

print("\n--- empty fingerprint issues a licence valid on any machine ---")
token2, _ = signing.issue(customer="Demo", fingerprint="", days=30)
status = licensing.verify(token2, fingerprint="anything-at-all")
check("valid on a machine it was never bound to", status.valid, True)

print("\n--- no --days means it never expires ---")
token3, _ = signing.issue(customer="Perpetual", fingerprint="fp9")
status = licensing.verify(token3, fingerprint="fp9")
check("valid", status.valid, True)
check("no expiry payload", status.payload.expires_at, None)
check("days_left is None (perpetual)", status.days_left, None)

print("\n--- the three validity bases are the panel's own fields ---")
import datetime as dt  # noqa: E402
import json  # noqa: E402
tok_vd, p_vd = signing.issue_payload(customer="نصب", fingerprint="fp9", valid_days=30)
parsed, body, _sig = licensing.parse_token(tok_vd)
check("from installation: vd set, no exp", (parsed.valid_days, parsed.expires_at), (30, None))
check("the payload has exactly LicensePayload.to_dict()'s keys and values",
      json.loads(body), parsed.to_dict())
check("not started yet: valid, no expiry counted", licensing.verify(tok_vd, fingerprint="fp9").days_left, None)
activated = dt.datetime.utcnow() - dt.timedelta(days=31)
check("after its term from the reported activation: expired",
      licensing.verify(tok_vd, fingerprint="fp9", activated_at=activated).reason, licensing.REASON_EXPIRED)
until = dt.datetime(2030, 3, 20, 20, 29, 59)
tok_until, _ = signing.issue_payload(customer="تاریخ", fingerprint="fp9", until=until)
check("until a date: that exact exp", licensing.parse_token(tok_until)[0].expires_at, until)
try:
    signing.issue_payload(customer="x", days=3, valid_days=3)
    check("two bases at once should be refused", "accepted", "ValueError")
except ValueError:
    check("two validity bases at once are refused", True, True)

print("\n--- read_token: only this server's own signature is accepted ---")
check("a token issued here reads back its payload", signing.read_token(tok_vd)["vd"], 30)
other_key = Ed25519PrivateKey.generate()
parts = tok_vd.split(".")
forged = parts[0] + "." + parts[1] + "." + signing._b64e(other_key.sign(signing._b64d(parts[1])))
for label, bad in (("a token signed by another key", forged), ("garbage", "NETCIP1.x.y"), ("empty", "")):
    try:
        signing.read_token(bad)
        check(f"{label} should be rejected", "accepted", "rejected")
    except signing.TokenRejected:
        check(f"{label} is rejected", True, True)
check("the public key shown is the one the panel verifies with", signing.public_key_b64(), pub_b64)

print("\n--- missing private key file fails loudly, not silently ---")
os.environ["LICENSE_PRIVATE_KEY_PATH"] = "/nonexistent/path/private.key"
import importlib
importlib.reload(signing)
check("has_signing_key is False", signing.has_signing_key(), False)
try:
    signing.issue(customer="x", fingerprint="")
    check("issue() should have raised", "no exception", "SigningKeyMissing")
except signing.SigningKeyMissing:
    check("issue() raises SigningKeyMissing when the key file is absent", True, True)

os.unlink(_key_path)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
