"""License verification - «ساختار لایسنسی».

Design decided with the panel owner 2026-08-27:

  * a license is a SIGNED, self-contained token. It verifies offline with a
    public key compiled into the panel; nothing has to be reachable for a
    valid license to be recognised as valid.
  * it is bound to the SERVER, by hardware fingerprint - not to an IP
    (VPS providers change those and the customer would be locked out for
    nothing) and not to a domain (plenty of installs run on a bare IP).
  * the online check exists only to REVOKE. A panel that cannot reach the
    licence server keeps working for GRACE_DAYS, then stops. So one outage
    on our side, or one afternoon of filtering, does not take every
    customer's panel down with it.

Two things this module deliberately does NOT try to be:

  * unbreakable. Anyone who owns the machine can eventually defeat any
    check that runs on that machine. The goal is to make copying an
    install to a second server a deliberate act of cracking rather than a
    convenience, and to make revocation possible.
  * clever. Every failure path has to be explainable to a customer on the
    phone, and every rejection says which of the four reasons it was.

The private signing key NEVER lives in this repository. Only the public
key does, below.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)

logger = logging.getLogger("licensing")

# Default for verify()'s lock_after_silent_days when the operator turns
# silence-locking ON. It is OFF by default (None) - see verify()'s
# docstring for why, given an all-Iran deployment. This constant is only
# the suggested value if they ever switch it on.
GRACE_DAYS = 7

# Public half of the signing key. The private half was generated once
# (2026-09-07) by scripts/license_tool.py and lives ONLY at
# .license-keys/netcip_license_private.key (gitignored - never in this
# public repo, never on a customer server).
#
# This used to be env-only and empty by default, on the reasoning that
# baking a key in would lock every not-yet-licensed panel the moment it
# updated. That reasoning was right about the consequence and wrong about
# the goal: a build with no key says "این نسخه بدون کنترل لایسنس ساخته شده
# است" and enforces nothing, so a customer install came up unlicensed and
# stayed that way. The requirement (2026-09) is the opposite - a fresh
# install should come up locked, say it has no licence, and offer the key
# form (routers/license.py's /activate). So the key IS compiled in, and the
# migration hazard is handled explicitly by LICENSE_MASTER_INSTALL below.
#
# The vendor's signing public key, compiled into the build.
#
# Empty means this build does not enforce licences AT ALL (verify() fails
# open - see its first check). Filling it in is what turns licensing on for
# every panel built from this source, which is the point: a customer's
# install should come up licensed-or-locked on its own, not depend on an
# env var they could simply delete.
#
# Safe to publish - it only VERIFIES signatures. The private key that mints
# them never leaves the vendor's machine (see scripts/license_tool.py).
#
# Read this before filling it in: every existing panel that has no licence
# key yet locks the moment it updates into a build with this set. That is
# the intended behaviour for customers, but it also covers YOUR OWN panels
# - set LICENSE_MASTER_INSTALL=true in their backend/.env (config.
# license_master_install), which exempts an install completely, or issue
# them keys first.
BUILTIN_SIGNING_PUBLIC_KEY_B64 = "Hi1lrjIXiXrW99rkumI8dsh2eTbD8ou-3SmHri5kuLo"

# A per-install override, kept so a panel can be pointed at a different
# signing key (a test key, a re-issued one) without a rebuild.
SIGNING_PUBLIC_KEY_B64 = os.environ.get(
    "USERMANAGER_LICENSE_PUBKEY", BUILTIN_SIGNING_PUBLIC_KEY_B64
)

TOKEN_PREFIX = "NETCIP1"


class LicenseError(Exception):
    """Raised only by issue/parse helpers. Verification never raises - it
    returns a LicenseStatus, because 'why is my panel locked' must always
    have an answer."""


# --------------------------------------------------------------------------
# hardware fingerprint
# --------------------------------------------------------------------------
# IMPORTANT: the panel runs inside a container, and a container's
# /etc/machine-id is NOT the host's - it changes on every rebuild, which
# would invalidate the licence on every update. docker-compose.yml
# bind-mounts the host's machine-id read-only at /host/etc/machine-id for
# exactly this reason; the container path is preferred and the local one is
# only a fallback for a bare-metal install.
_HOST_MACHINE_ID_PATHS = (
    "/host/etc/machine-id",
    "/etc/machine-id",
    "/var/lib/dbus/machine-id",
)

# Same reasoning, same bug class, found 2026-09-07: _primary_mac() below
# needs the HOST's network interfaces, not the container's own - a
# container's /sys/class/net is regenerated (fresh veth pair, fresh MAC)
# on every recreate, same as its /etc/machine-id would be without the
# mount above. Unlike machine-id this one was never mounted at all, so
# the fingerprint silently included a genuinely random component on every
# single `docker compose up --force-recreate` - which is every routine
# update. A licence issued right after one recreate mismatched the very
# next one: REASON_WRONG_MACHINE, panel locked, no code bug in the
# licence itself, just a fingerprint quietly built from the wrong
# filesystem.
#
# UPDATE 2026-09-08: mounting only the host's /sys/class/net (as
# docker-compose.yml used to) was STILL not enough on its own - every
# entry under /sys/class/net is a symlink with a RELATIVE target that
# climbs back out of that directory (e.g. ens160 -> ../../devices/
# pci0000:00/.../net/ens160), which only resolves if /sys/devices is
# ALSO reachable at the same relative position. `ls` on the symlink
# looks completely fine either way (it just prints the link text, never
# resolves it) - only actually opening the file to read the MAC exposes
# the break, so this looked fixed and wasn't. docker-compose.yml now
# mounts the whole host /sys tree read-only at /host/sys (still just
# device/network metadata, nothing writable, nothing secret) so these
# relative symlinks resolve for real. _primary_mac() itself is
# unchanged below - the container path (still /host/sys/class/net,
# now reachable) is preferred, the local one is only a fallback for a
# bare-metal install (module constant, not inlined in the function,
# specifically so a test can point it at a fake filesystem).
_HOST_NET_CLASS_PATHS = (
    "/host/sys/class/net",
    "/sys/class/net",
)


def _read_first(paths) -> str:
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                return value
        except OSError:
            continue
    return ""


def _cpu_signature() -> str:
    """Model name + core count. Stable across reboots, changes if the VPS is
    genuinely rebuilt on different hardware - which is when we DO want to
    re-issue."""
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return ""
    model = ""
    cores = 0
    for line in text.splitlines():
        if line.startswith("model name") and not model:
            model = line.split(":", 1)[-1].strip()
        if line.startswith("processor"):
            cores += 1
    return f"{model}|{cores}"


def _primary_mac() -> str:
    """The MAC of the host's main interface. Read from /host/sys when the
    bind mount is present, since a container's own interfaces are
    generated fresh every start."""
    for base in _HOST_NET_CLASS_PATHS:
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            if name == "lo" or name.startswith(("docker", "br-", "veth", "wg")):
                continue
            mac = _read_first([os.path.join(base, name, "address")])
            if mac and mac != "00:00:00:00:00:00":
                return mac
    return ""


def hardware_fingerprint() -> str:
    """A stable id for THIS server.

    Deliberately built from several weak signals rather than one strong
    one: any single file can be faked trivially, and any single one can
    also legitimately change. Hashing the combination means a customer
    moving to genuinely different hardware needs a new licence (which is
    the point), while a reboot, a container rebuild or a new IP does not.

    Never returns empty: if every source fails, a random id is generated
    and stored, so the panel still has SOMETHING to bind to instead of
    every install sharing one blank fingerprint.
    """
    parts = [
        _read_first(_HOST_MACHINE_ID_PATHS),
        _cpu_signature(),
        _primary_mac(),
    ]
    if not any(parts):
        parts = [_persistent_fallback_id()]
    raw = "||".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _persistent_fallback_id() -> str:
    """Last resort - a random id kept next to the database so it survives
    restarts. Weak on purpose: it is better than binding every unknown
    machine to the same empty string."""
    path = os.environ.get("USERMANAGER_FALLBACK_ID_PATH", "/app/data/.machine")
    existing = _read_first([path])
    if existing:
        return existing
    value = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError:
        logger.warning("could not persist fallback machine id at %s", path)
    return value


# --------------------------------------------------------------------------
# token format
# --------------------------------------------------------------------------
def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass
class LicensePayload:
    """What a licence actually says."""

    license_id: str
    customer: str
    fingerprint: str          # "" = not bound to a machine (a demo/dev key)
    issued_at: dt.datetime
    expires_at: Optional[dt.datetime]   # None = perpetual (or see valid_days)
    max_customers: Optional[int] = None
    features: list[str] = field(default_factory=list)
    note: str = ""
    # Runs for this many days from the moment it is first ACTIVATED on a
    # panel, rather than from a date fixed at issue time.
    #
    # A key with a baked-in `exp` starts burning the day it is minted, so a
    # customer who installs a week later silently loses that week - and a
    # key cut in advance for someone who has not set their server up yet can
    # be half gone before it is ever used (panel owner, 2026-09: "کد لایسنس
    # از زمان نصب فعال باشه"). With valid_days the clock starts when the
    # panel first sees the key; see effective_expiry() and
    # services/license_state.py, which is where the activation date is
    # recorded and anchored.
    #
    # `exp` still wins when both are set, so an absolute deadline can always
    # be imposed on top ("valid for 30 days from install, but never past
    # Nowruz").
    valid_days: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "v": 1,
            "id": self.license_id,
            "customer": self.customer,
            "fp": self.fingerprint,
            "iat": self.issued_at.replace(microsecond=0).isoformat(),
            "exp": self.expires_at.replace(microsecond=0).isoformat() if self.expires_at else None,
            "max_customers": self.max_customers,
            "features": self.features,
            "note": self.note,
            "vd": self.valid_days,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LicensePayload":
        def _parse(value):
            return dt.datetime.fromisoformat(value) if value else None

        return cls(
            license_id=data["id"],
            customer=data.get("customer", ""),
            fingerprint=data.get("fp", ""),
            issued_at=_parse(data.get("iat")) or dt.datetime.utcnow(),
            expires_at=_parse(data.get("exp")),
            max_customers=data.get("max_customers"),
            features=list(data.get("features") or []),
            note=data.get("note", ""),
            valid_days=data.get("vd"),
        )


def effective_expiry(
    payload: "LicensePayload", activated_at: Optional[dt.datetime]
) -> Optional[dt.datetime]:
    """When this licence actually runs out. None = never.

    An absolute `exp` always wins, so a hard deadline can be layered on top
    of a duration. Otherwise a `valid_days` key expires that many days after
    `activated_at` - the moment the panel first saw it.

    A valid_days key that has NOT been activated yet has no expiry to report
    (None): it has not started. The caller activates it, which is what puts
    a date on the clock - see services/license_state.py.
    """
    if payload.expires_at is not None:
        return payload.expires_at
    if payload.valid_days and activated_at is not None:
        return activated_at + dt.timedelta(days=int(payload.valid_days))
    return None


def parse_token(token: str) -> tuple[LicensePayload, bytes, bytes]:
    """token -> (payload, signed_bytes, signature). Does NOT verify."""
    token = (token or "").strip().replace("\n", "").replace(" ", "")
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        raise LicenseError("قالب کلید لایسنس درست نیست")
    try:
        body = _b64d(parts[1])
        signature = _b64d(parts[2])
        payload = LicensePayload.from_dict(json.loads(body.decode("utf-8")))
    except LicenseError:
        raise
    except Exception as exc:  # noqa: BLE001 - malformed input of any shape
        raise LicenseError("کلید لایسنس خوانده نشد") from exc
    return payload, body, signature


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------
REASON_OK = "ok"
REASON_MISSING = "missing"
REASON_MALFORMED = "malformed"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_WRONG_MACHINE = "wrong_machine"
REASON_EXPIRED = "expired"
REASON_REVOKED = "revoked"
REASON_GRACE_EXHAUSTED = "grace_exhausted"
REASON_CLOCK = "clock"

_MESSAGES = {
    REASON_MISSING: "لایسنسی ثبت نشده است",
    REASON_MALFORMED: "کلید لایسنس خوانده نشد",
    REASON_BAD_SIGNATURE: "امضای لایسنس معتبر نیست",
    REASON_WRONG_MACHINE: "این لایسنس برای سرور دیگری صادر شده است",
    REASON_EXPIRED: "اعتبار لایسنس تمام شده است",
    REASON_REVOKED: "این لایسنس لغو شده است",
    REASON_GRACE_EXHAUSTED: (
        "بیش از {days} روز است که ارتباط با سرور لایسنس برقرار نشده"
    ),
    REASON_CLOCK: "ساعت سرور عقب‌تر از آخرین بررسی است",
}


@dataclass
class LicenseStatus:
    valid: bool
    reason: str
    payload: Optional[LicensePayload] = None
    message: str = ""
    days_left: Optional[int] = None          # until the licence expires
    grace_days_left: Optional[int] = None    # until an unreachable server locks it

    @property
    def in_grace(self) -> bool:
        return self.valid and self.grace_days_left is not None


def _public_key() -> Optional[Ed25519PublicKey]:
    if not SIGNING_PUBLIC_KEY_B64:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(_b64d(SIGNING_PUBLIC_KEY_B64))
    except Exception:  # noqa: BLE001
        logger.error("the built-in licence public key is not readable")
        return None


def verify(
    token: str,
    *,
    fingerprint: Optional[str] = None,
    now: Optional[dt.datetime] = None,
    last_online_check: Optional[dt.datetime] = None,
    revoked: bool = False,
    highest_seen_time: Optional[dt.datetime] = None,
    lock_after_silent_days: Optional[int] = None,
    activated_at: Optional[dt.datetime] = None,
) -> LicenseStatus:
    """The whole decision, in one pure function.

    Everything it needs is passed in, so it can be tested exhaustively
    without a database, a clock or a network - and so that the enforcement
    layer above has no room to disagree with it.

    `highest_seen_time` is the latest timestamp this install has ever
    recorded. If the wall clock is now well behind that, someone has moved
    the clock back - the oldest trick for extending an expired licence.

    `lock_after_silent_days` decides what silence means:

      * None (the default): silence NEVER locks. Payment is enforced by the
        licence's own expiry (a monthly key you re-issue on payment), and
        the online heartbeat only ever REVOKES. Chosen for this deployment
        because every panel AND the control server sit on Iranian servers:
        panel-to-control traffic is domestic and reliable, so revocation is
        usually instant anyway - but the control server itself going down
        (hardware, a datacentre outage) must never lock a customer who has
        already paid. With this None, it cannot.

      * a number N: the old behaviour - if the panel has not reached the
        control server in N days it locks. Available if the operator ever
        wants it, but off by default for the reason above.

    `activated_at` is when this install first saw this key, and only matters
    for a licence issued with `valid_days` (one whose clock starts at
    installation rather than at issue - see effective_expiry). Passing None
    for such a key means "not started yet", which never expires here; the
    caller is expected to stamp the activation and pass it in from then on.
    """
    now = now or dt.datetime.utcnow()

    # No public key compiled in => this build does not enforce licences at
    # all. This is checked FIRST, before the token is even looked at,
    # because right now no key is baked in and every panel would otherwise
    # lock itself at login over a missing/garbage token. A build with no key
    # is a development or not-yet-licensed build, and locking it could only
    # ever lock US out - so it fails OPEN regardless of what the token is.
    key = _public_key()
    if key is None:
        return LicenseStatus(True, REASON_OK, payload=None,
                             message="این نسخه بدون کنترل لایسنس ساخته شده است")

    if not (token or "").strip():
        return LicenseStatus(False, REASON_MISSING, message=_MESSAGES[REASON_MISSING])

    try:
        payload, body, signature = parse_token(token)
    except LicenseError as exc:
        return LicenseStatus(False, REASON_MALFORMED, message=str(exc))

    try:
        key.verify(signature, body)
    except InvalidSignature:
        return LicenseStatus(False, REASON_BAD_SIGNATURE,
                             message=_MESSAGES[REASON_BAD_SIGNATURE])

    # Signature is good, so from here on the payload can be trusted.
    if revoked:
        return LicenseStatus(False, REASON_REVOKED, payload=payload,
                             message=_MESSAGES[REASON_REVOKED])

    # Clock rolled back below anything we have already seen.
    if highest_seen_time and now < highest_seen_time - dt.timedelta(hours=25):
        return LicenseStatus(False, REASON_CLOCK, payload=payload,
                             message=_MESSAGES[REASON_CLOCK])

    if payload.fingerprint:
        actual = fingerprint if fingerprint is not None else hardware_fingerprint()
        if actual != payload.fingerprint:
            return LicenseStatus(False, REASON_WRONG_MACHINE, payload=payload,
                                 message=_MESSAGES[REASON_WRONG_MACHINE])

    days_left = None
    expires_at = effective_expiry(payload, activated_at)
    if expires_at:
        if expires_at <= now:
            return LicenseStatus(False, REASON_EXPIRED, payload=payload,
                                 message=_MESSAGES[REASON_EXPIRED])
        days_left = (expires_at - now).days

    # The licence itself is fine. Silence only matters if the operator has
    # explicitly opted into locking on it (lock_after_silent_days). By
    # default it does not, so a control-server outage never touches a paid
    # customer - see this function's docstring.
    grace_days_left = None
    if lock_after_silent_days and last_online_check is not None:
        offline_for = now - last_online_check
        if offline_for > dt.timedelta(days=lock_after_silent_days):
            return LicenseStatus(
                False, REASON_GRACE_EXHAUSTED, payload=payload,
                message=_MESSAGES[REASON_GRACE_EXHAUSTED].format(days=lock_after_silent_days),
                days_left=days_left,
            )
        if offline_for > dt.timedelta(hours=24):
            grace_days_left = lock_after_silent_days - offline_for.days

    return LicenseStatus(True, REASON_OK, payload=payload, message="",
                         days_left=days_left, grace_days_left=grace_days_left)


# --------------------------------------------------------------------------
# what a lock actually closes - configurable per customer
# --------------------------------------------------------------------------
# The panel owner chooses, per install, how hard a lock bites. The default
# is the gentlest that still applies real pressure: shut the reseller out of
# their own panel, but leave their END CUSTOMERS' VPN running. Those
# customers paid the reseller, not us; cutting them off punishes the wrong
# people and earns the operator a bad name for an unpaid invoice that is not
# theirs.
SCOPE_PANEL_ONLY = "panel_only"          # reseller can't log in; everything else runs
SCOPE_PANEL_AND_BOT = "panel_and_bot"    # + the sales bot stops taking orders
SCOPE_EVERYTHING = "everything"          # + end customers' connections are cut

LOCK_SCOPES = (SCOPE_PANEL_ONLY, SCOPE_PANEL_AND_BOT, SCOPE_EVERYTHING)
DEFAULT_LOCK_SCOPE = SCOPE_PANEL_ONLY


@dataclass
class Enforcement:
    """The concrete consequences of a status, once the operator's chosen
    scope is applied. A boolean per thing that can be shut off, so the
    call sites stay dumb: each just reads the flag that concerns it and
    never re-derives the policy."""

    lock_panel: bool = False        # block admin/reseller login
    lock_bot: bool = False          # sales bot stops selling
    cut_services: bool = False      # end customers' VPN is disabled
    reason: str = REASON_OK
    message: str = ""
    days_left: Optional[int] = None
    grace_days_left: Optional[int] = None

    @property
    def any_lock(self) -> bool:
        return self.lock_panel or self.lock_bot or self.cut_services


def resolve_enforcement(status: LicenseStatus, lock_scope: str = DEFAULT_LOCK_SCOPE) -> Enforcement:
    """Turn a verdict + the operator's chosen severity into a set of flags.

    Kept separate from verify() on purpose: WHETHER the licence is good is a
    cryptographic fact with one right answer, but HOW MUCH to close when it
    is bad is the operator's policy choice. Mixing them would make the
    policy untestable and tempt a caller into inventing its own.

    A valid licence closes nothing, whatever the scope - scope only ever
    decides how far an INVALID one reaches.
    """
    if status.valid:
        return Enforcement(
            reason=status.reason, message=status.message,
            days_left=status.days_left, grace_days_left=status.grace_days_left,
        )

    if lock_scope not in LOCK_SCOPES:
        lock_scope = DEFAULT_LOCK_SCOPE

    # Every level of lock includes the levels gentler than it.
    lock_panel = True
    lock_bot = lock_scope in (SCOPE_PANEL_AND_BOT, SCOPE_EVERYTHING)
    cut_services = lock_scope == SCOPE_EVERYTHING

    return Enforcement(
        lock_panel=lock_panel,
        lock_bot=lock_bot,
        cut_services=cut_services,
        reason=status.reason,
        message=status.message,
        days_left=status.days_left,
    )
