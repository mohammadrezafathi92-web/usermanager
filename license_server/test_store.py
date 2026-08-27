"""The licence server's core, checked without any HTTP.

Run:  python3 license_server/test_store.py

The two things that matter most here:
  - a heartbeat returns exactly what the install should do, and nothing
    more the install could misread;
  - the "copied to a second server" signal is actually raised, because
    that is the whole reason an operator watches this at all.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import store

failures: list[str] = []
NOW = dt.datetime(2026, 8, 27, 12, 0, 0)


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def fresh_db():
    return store.make_session_factory("sqlite://")()


print("--- a brand-new install self-registers as allowed ---")
db = fresh_db()
inst, resp = store.record_heartbeat(db, license_id="lic_a", fingerprint="fp1",
                                    ip="1.2.3.4", panel_version="1.3.0", now=NOW)
check("it is recorded", store.get_install(db, "lic_a") is not None, True)
check("...and told it is NOT revoked", resp["revoked"], False)
check("...with the default gentle scope", resp["lock_scope"], store.SCOPE_PANEL_ONLY)
check("its fingerprint was stored", inst.fingerprint, "fp1")
check("its ip was stored", inst.last_ip, "1.2.3.4")
check("its version was stored", inst.panel_version, "1.3.0")
check("heartbeat counted", inst.heartbeat_count, 1)

print("\n--- repeated heartbeats update, not duplicate ---")
inst, _ = store.record_heartbeat(db, license_id="lic_a", fingerprint="fp1",
                                 now=NOW + dt.timedelta(hours=6))
check("still one row", len(store.list_installs(db)), 1)
check("count went up", inst.heartbeat_count, 2)
check("last_seen advanced", inst.last_seen, NOW + dt.timedelta(hours=6))
check("first_seen unchanged", inst.first_seen, NOW)

print("\n--- the copy signal: same licence, new fingerprint ---")
inst, _ = store.record_heartbeat(db, license_id="lic_a", fingerprint="fp2_DIFFERENT",
                                 now=NOW + dt.timedelta(days=1))
check("the change was timestamped", inst.fingerprint_changed_at, NOW + dt.timedelta(days=1))
check("the new fingerprint is stored", inst.fingerprint, "fp2_DIFFERENT")
# A heartbeat that keeps the same fingerprint must NOT keep re-flagging.
inst, _ = store.record_heartbeat(db, license_id="lic_a", fingerprint="fp2_DIFFERENT",
                                 now=NOW + dt.timedelta(days=2))
check("an unchanged fingerprint does not re-flag", inst.fingerprint_changed_at,
      NOW + dt.timedelta(days=1))

print("\n--- revoking flips what the heartbeat returns ---")
db = fresh_db()
store.record_heartbeat(db, license_id="lic_b", fingerprint="x", now=NOW)
store.set_revoked(db, "lic_b", True)
_, resp = store.record_heartbeat(db, license_id="lic_b", fingerprint="x", now=NOW)
check("a revoked install is told so", resp["revoked"], True)
store.set_revoked(db, "lic_b", False)
_, resp = store.record_heartbeat(db, license_id="lic_b", fingerprint="x", now=NOW)
check("un-revoking restores it", resp["revoked"], False)

print("\n--- the lock scope the operator picks is what the install is told ---")
db = fresh_db()
store.record_heartbeat(db, license_id="lic_c", fingerprint="x", now=NOW)
for scope in store.LOCK_SCOPES:
    store.set_lock_scope(db, "lic_c", scope)
    _, resp = store.record_heartbeat(db, license_id="lic_c", fingerprint="x", now=NOW)
    check(f"scope {scope} is echoed back", resp["lock_scope"], scope)

try:
    store.set_lock_scope(db, "lic_c", "nonsense")
    check("an invalid scope is rejected", "did not raise", "should have raised")
except ValueError:
    check("an invalid scope is rejected", True, True)

print("\n--- the response is ONLY the two fields the install acts on ---")
db = fresh_db()
_, resp = store.record_heartbeat(db, license_id="lic_d", fingerprint="x", now=NOW)
check("exactly revoked + lock_scope, nothing else", sorted(resp.keys()), ["lock_scope", "revoked"])

print("\n--- labelling and notes ---")
db = fresh_db()
store.record_heartbeat(db, license_id="lic_e", fingerprint="x", now=NOW)
store.set_label(db, "lic_e", "فروشگاه رضا", note="ماهانه، تسویه ۱ هر ماه")
inst = store.get_install(db, "lic_e")
check("label saved (Persian)", inst.label, "فروشگاه رضا")
check("note saved", inst.note, "ماهانه، تسویه ۱ هر ماه")

print("\n--- forgetting an install ---")
check("forget returns True", store.forget_install(db, "lic_e"), True)
check("...and the row is gone", store.get_install(db, "lic_e"), None)
check("forgetting a stranger returns False", store.forget_install(db, "nope"), False)
# It re-appears if it heartbeats again - forget is not block.
store.record_heartbeat(db, license_id="lic_e", fingerprint="x", now=NOW)
check("a forgotten install re-registers on its next heartbeat",
      store.get_install(db, "lic_e") is not None, True)

print("\n--- operator actions on an unknown licence don't crash ---")
check("revoke unknown", store.set_revoked(db, "ghost", True), None)
check("scope unknown", store.set_lock_scope(db, "ghost", store.SCOPE_EVERYTHING), None)
check("label unknown", store.set_label(db, "ghost", "x"), None)

print("\n--- list order: most recently seen first ---")
db = fresh_db()
store.record_heartbeat(db, license_id="old", fingerprint="x", now=NOW)
store.record_heartbeat(db, license_id="new", fingerprint="x", now=NOW + dt.timedelta(hours=1))
check("newest install is first", [i.license_id for i in store.list_installs(db)], ["new", "old"])

print("\n--- a missing license_id is refused ---")
try:
    store.record_heartbeat(db, license_id="", fingerprint="x")
    check("empty license_id rejected", "did not raise", "should have raised")
except ValueError:
    check("empty license_id rejected", True, True)

print("\n--- the console password ---")
db = fresh_db()
first = store.ensure_admin_password(db, "secret-123")
check("first run sets the given password", first, "secret-123")
check("...and it verifies", store.check_admin_password(db, "secret-123"), True)
check("...a wrong password does not", store.check_admin_password(db, "wrong"), False)
second = store.ensure_admin_password(db, "different-now")
check("a second run does NOT overwrite it", second, "(existing)")
check("...the original still works", store.check_admin_password(db, "secret-123"), True)
check("...the ignored one does not", store.check_admin_password(db, "different-now"), False)
store.set_admin_password(db, "rotated")
check("rotating changes it", store.check_admin_password(db, "rotated"), True)
check("...and the old one stops working", store.check_admin_password(db, "secret-123"), False)
# The password is never stored in the clear.
raw = store._get_setting(db, "admin_password_hash")
check("stored as a hash, not plaintext", "rotated" not in (raw or ""), True)

print("\n--- our scope list matches the panel's ---")
# The panel and this service each keep their own copy of the scope names
# (they ship separately). If they ever drift, an operator picks a scope the
# panel does not understand. This asserts they are identical.
PANEL_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, PANEL_ROOT)
try:
    from app.services import licensing as panel_licensing
    check("scope lists are identical", set(store.LOCK_SCOPES), set(panel_licensing.LOCK_SCOPES))
    check("default scope matches", store.DEFAULT_LOCK_SCOPE, panel_licensing.DEFAULT_LOCK_SCOPE)
except Exception as exc:  # noqa: BLE001 - panel deps may be absent in a standalone build
    print(f"SKIP  panel comparison ({type(exc).__name__}: {exc})")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
