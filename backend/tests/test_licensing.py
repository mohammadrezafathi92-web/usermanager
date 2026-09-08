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

print("\n--- silence NEVER locks by default (control server may be down) ---")
# The whole point of the all-Iran decision: our control server going down
# must not lock a paying customer. Payment is enforced by the licence's own
# expiry; the heartbeat only revokes.
token = issue()
for label, ago in [("an hour", dt.timedelta(hours=1)),
                   ("3 days", dt.timedelta(days=3)),
                   ("30 days", dt.timedelta(days=30)),
                   ("a year", dt.timedelta(days=365))]:
    s = verdict(token, last_online_check=NOW - ago)
    check(f"offline for {label}: still works", s.valid, True)
s = verdict(token, last_online_check=None)
check("never checked online at all: still works", s.valid, True)

print("\n--- but silence-locking can be turned ON if the operator wants it ---")
s = verdict(token, last_online_check=NOW - dt.timedelta(hours=1),
            lock_after_silent_days=7)
check("checked an hour ago: fine, no warning", (s.valid, s.grace_days_left), (True, None))

s = verdict(token, last_online_check=NOW - dt.timedelta(days=3),
            lock_after_silent_days=7)
check("offline 3 days: still working", s.valid, True)
check("...and warns with days remaining", s.grace_days_left, 4)
check("...flagged as in grace", s.in_grace, True)

s = verdict(token, last_online_check=NOW - dt.timedelta(days=7, hours=-1),
            lock_after_silent_days=7)
check("just inside the window: still working", s.valid, True)

s = verdict(token, last_online_check=NOW - dt.timedelta(days=7, hours=1),
            lock_after_silent_days=7)
check("just past it: locked", s.valid, False)
check("...for the grace reason, not a licence fault", s.reason, licensing.REASON_GRACE_EXHAUSTED)
check("...and the message names the number of days", "7" in s.message, True)

# An expired LICENCE still stops immediately, silence-locking or not -
# that is the payment channel and it is always on.
s = verdict(issue(expires_in_days=-1), last_online_check=NOW - dt.timedelta(hours=1))
check("an expired licence stops even with silence-locking off", s.valid, False)
check("...for expiry, the always-on payment reason", s.reason, licensing.REASON_EXPIRED)

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

print("\n--- the MAC component must come from the HOST, not the container ---")
# Bug found 2026-09-07: _primary_mac() prefers _HOST_NET_CLASS_PATHS[0]
# (bind-mounted from the host) but falls back to the container's OWN
# /sys/class/net when that mount is missing - and a container's own
# network interfaces are regenerated (fresh MAC) on every recreate. A
# panel with no host mount therefore got a NEW fingerprint on every
# `docker compose up --force-recreate`, silently mismatching any licence
# issued moments earlier. This simulates "recreate" as exactly that: the
# would-be-container-local path's content changing while the host path's
# content does not - the fingerprint must follow the host path only.
import tempfile
import shutil

host_net = tempfile.mkdtemp()
container_net = tempfile.mkdtemp()


def _make_iface(base, name, mac):
    d = os.path.join(base, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "address"), "w") as fh:
        fh.write(mac + "\n")


_make_iface(host_net, "eth0", "aa:bb:cc:dd:ee:01")
_make_iface(container_net, "eth0", "de:ad:be:ef:00:01")  # "first boot"'s veth

original_net_paths = licensing._HOST_NET_CLASS_PATHS
licensing._HOST_NET_CLASS_PATHS = (host_net, container_net)
try:
    mac_before = licensing._primary_mac()
    check("reads the HOST path when both are present", mac_before, "aa:bb:cc:dd:ee:01")

    # Simulate a container recreate: the "container-local" path's veth got
    # a brand new MAC, but the host path (a real bind mount) is untouched.
    shutil.rmtree(container_net)
    os.makedirs(container_net)
    _make_iface(container_net, "eth1", "11:22:33:44:55:66")  # different veth entirely

    mac_after = licensing._primary_mac()
    check("...and is unaffected by the 'container' side changing", mac_after, mac_before)

    fp_before = licensing.hardware_fingerprint()
    fp_after = licensing.hardware_fingerprint()
    check("hardware_fingerprint() is therefore stable across the 'recreate' too", fp_before, fp_after)

    # And the actual failure mode this caused: NO host mount at all (path
    # doesn't exist), so it falls through to the container-local path -
    # which is exactly the pre-fix, unmounted production behaviour.
    licensing._HOST_NET_CLASS_PATHS = ("/no/such/host/mount", container_net)
    mac_unmounted_1 = licensing._primary_mac()
    shutil.rmtree(container_net)
    os.makedirs(container_net)
    _make_iface(container_net, "vethNEW", "99:88:77:66:55:44")
    mac_unmounted_2 = licensing._primary_mac()
    check("without the host mount, it DOES drift across a simulated recreate - reproducing the bug",
          mac_unmounted_1 == mac_unmounted_2, False)
finally:
    licensing._HOST_NET_CLASS_PATHS = original_net_paths
    shutil.rmtree(host_net, ignore_errors=True)
    shutil.rmtree(container_net, ignore_errors=True)

print("\n--- mounting ONLY .../class/net is not enough - the symlinks inside it escape the mount ---")
# Second bug found 2026-09-08, on top of the one above: every real entry
# under a real /sys/class/net is a symlink with a RELATIVE target that
# climbs back OUT of that directory, e.g.
#   ens160 -> ../../devices/pci0000:00/0000:00:15.0/0000:03:00.0/net/ens160
# docker-compose.yml used to bind-mount only the host's /sys/class/net
# (not the rest of /sys) at the container path _HOST_NET_CLASS_PATHS[0]
# points at - which means that relative target resolves to a path that
# was NEVER mounted (.../devices/... sits outside /sys/class/net
# entirely), so actually reading the MAC through the symlink fails even
# though `os.listdir` and `ls` on the symlink itself look completely
# normal (listing a symlink never has to resolve it). _primary_mac()
# silently fell through to the next entry, then to the container-local
# path - reproducing the exact original bug via a DIFFERENT root cause.
# Confirmed against the real deployment: `cat .../ens160/address` failed
# with ENOENT while `ls -la` on the same symlink showed nothing wrong.
fake_root = tempfile.mkdtemp()  # simulates mounting the WHOLE host /sys
broken_leaf = tempfile.mkdtemp()  # simulates mounting ONLY .../class/net
container_net2 = tempfile.mkdtemp()
_make_iface(container_net2, "eth0", "de:ad:be:ef:00:02")


def _make_real_sysfs_iface(sys_root, name, mac):
    """Builds `<sys_root>/class/net/<name>` as a symlink pointing at
    `<sys_root>/devices/<name>/net/<name>` via the SAME 2-levels-up
    relative form the real kernel uses, plus the real device directory it
    points to - i.e. a faithful mini sysfs, not a shortcut."""
    class_net = os.path.join(sys_root, "class", "net")
    os.makedirs(class_net, exist_ok=True)
    device_dir = os.path.join(sys_root, "devices", name, "net", name)
    os.makedirs(device_dir, exist_ok=True)
    with open(os.path.join(device_dir, "address"), "w") as fh:
        fh.write(mac + "\n")
    os.symlink(os.path.join("..", "..", "devices", name, "net", name), os.path.join(class_net, name))


_make_real_sysfs_iface(fake_root, "ens160", "00:50:56:98:cf:c4")

# The broken mount: ONLY the "class/net" leaf exists on this side, same
# symlink target string as above, but its ".." parents lead nowhere real.
os.symlink(
    os.path.join("..", "..", "devices", "ens160", "net", "ens160"),
    os.path.join(broken_leaf, "ens160"),
)

try:
    licensing._HOST_NET_CLASS_PATHS = (broken_leaf, container_net2)
    check(
        "mounting only the leaf dir: the symlink can't resolve, falls through to the container path (the bug)",
        licensing._primary_mac(), "de:ad:be:ef:00:02",
    )

    licensing._HOST_NET_CLASS_PATHS = (os.path.join(fake_root, "class", "net"), container_net2)
    check(
        "mounting the whole tree: the same relative symlink resolves, reads the real host MAC (the fix)",
        licensing._primary_mac(), "00:50:56:98:cf:c4",
    )
finally:
    licensing._HOST_NET_CLASS_PATHS = original_net_paths
    shutil.rmtree(fake_root, ignore_errors=True)
    shutil.rmtree(broken_leaf, ignore_errors=True)
    shutil.rmtree(container_net2, ignore_errors=True)

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
