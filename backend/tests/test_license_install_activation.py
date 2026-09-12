"""A licence can start counting at INSTALLATION instead of at issue.

Run:  python3 backend/tests/test_license_install_activation.py

Asked for 2026-09 ("کد لایسنس از زمان نصب فعال باشه"). A key issued with a
baked-in expiry starts burning the moment it is minted: a customer who
installs a week later silently loses that week, and a key cut in advance
for a server that is not ready yet can be half gone before it is ever used.

A key issued with `valid_days` carries no date at all. The panel stamps the
start the first time it sees the key, and the term runs from there.

The obvious way to cheat it is to delete the panel's local state file and
get a fresh term, so the activation date is also reported to the control
server, which keeps the EARLIEST it has ever seen and hands it back.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import licensing

failures: list[str] = []
NOW = dt.datetime(2026, 9, 12, 12, 0, 0)


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def payload(**kw):
    base = dict(
        license_id="lic_test", customer="c", fingerprint="",
        issued_at=NOW - dt.timedelta(days=30), expires_at=None,
    )
    base.update(kw)
    return licensing.LicensePayload(**base)


print("--- effective_expiry ---")
check("a fixed expiry is used as-is",
      licensing.effective_expiry(payload(expires_at=NOW + dt.timedelta(days=5)), None),
      NOW + dt.timedelta(days=5))
check("a perpetual key never expires", licensing.effective_expiry(payload(), None), None)
check("an install-dated key that was never activated has no expiry yet",
      licensing.effective_expiry(payload(valid_days=30), None), None)
check("...and once activated, runs from THAT moment",
      licensing.effective_expiry(payload(valid_days=30), NOW), NOW + dt.timedelta(days=30))
check("issue date is irrelevant to it",
      licensing.effective_expiry(payload(valid_days=30, issued_at=NOW - dt.timedelta(days=300)), NOW),
      NOW + dt.timedelta(days=30))
check("a fixed expiry still wins when both are set - a hard deadline on top",
      licensing.effective_expiry(
          payload(valid_days=365, expires_at=NOW + dt.timedelta(days=3)), NOW),
      NOW + dt.timedelta(days=3))

print("\n--- the token survives a round trip ---")
p = payload(valid_days=45)
restored = licensing.LicensePayload.from_dict(p.to_dict())
check("valid_days is carried in the signed body", restored.valid_days, 45)
check("an old key without the field still parses",
      licensing.LicensePayload.from_dict(
          {k: v for k, v in p.to_dict().items() if k != "vd"}).valid_days,
      None)

print("\n--- the control server keeps the earliest activation ---")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "license_server"))
import store  # noqa: E402

Session = store.make_session_factory("sqlite://")
db = Session()

first = NOW - dt.timedelta(days=20)
store.record_heartbeat(db, license_id="lic_test", activated_at=first, now=NOW)
_i, resp = store.record_heartbeat(db, license_id="lic_test", activated_at=first, now=NOW)
check("the activation it was told is stored and handed back",
      resp["activated_at"], first.isoformat())

# The cheat: wipe the panel's state file and it reports "activated today".
_i, resp = store.record_heartbeat(db, license_id="lic_test", activated_at=NOW, now=NOW)
check("a later date cannot extend the term", resp["activated_at"], first.isoformat())

# A genuine correction backwards is accepted.
earlier = NOW - dt.timedelta(days=40)
_i, resp = store.record_heartbeat(db, license_id="lic_test", activated_at=earlier, now=NOW)
check("an earlier date does correct it", resp["activated_at"], earlier.isoformat())

check("a licence that never reported one has none",
      store.record_heartbeat(db, license_id="lic_other", now=NOW)[1]["activated_at"], None)


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("لایسنس از لحظه‌ی نصب شروع می‌شود، نه از لحظه‌ی صدور")
