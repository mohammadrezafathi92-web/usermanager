"""ip_guard: an IP that keeps hitting the API with no/bad credentials gets
permanently blocked - "بعد از ۱۰ ریکوست" for an unknown caller, "بعد از ۲۰
ریکوست پست" for one presenting a rejected credential.

Run:  python3 backend/tests/test_ip_guard.py

Two layers are tested: the counting/banning rules in services/ip_guard.py
directly (fast, precise about the thresholds), and the actual middleware
body (guard_request) against a small real FastAPI app + TestClient, so the
wiring itself (banned IP short-circuits before the route runs, OPTIONS is
exempt, /api/auth/login is excluded) is proven end-to-end rather than
assumed.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import ip_guard

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def fresh_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def reset_ip_guard_state():
    """Every test needs a clean slate - ip_guard's counters/ban set are
    module-level (deliberately, for hot-path speed in production)."""
    ip_guard._banned_ips.clear()
    ip_guard._unknown_hits.clear()
    ip_guard._post_bad_cred_hits.clear()
    ip_guard._radius_unknown_hits.clear()
    ip_guard._radius_inactive_hits.clear()


print("--- unknown caller (no credential at all): banned at exactly 10 ---")
reset_ip_guard_state()
db = fresh_db()
banned_at = None
for i in range(1, 13):
    just_banned = ip_guard.record_failure(db, "1.2.3.4", "GET", "/api/users", had_credential=False)
    if just_banned and banned_at is None:
        banned_at = i
check("bans on the 10th request, not before/after", banned_at, ip_guard.UNKNOWN_LIMIT)
check("is_banned() reflects it immediately", ip_guard.is_banned("1.2.3.4"), True)
row = db.query(models.IpBan).filter(models.IpBan.ip == "1.2.3.4").first()
check("...and it's persisted", row is not None, True)
check("...auto-ban, not manual", row.is_manual, False)
check("...with the trip count recorded", row.hit_count, ip_guard.UNKNOWN_LIMIT)

print("\n--- a POST with a REJECTED credential: banned at exactly 20, not 10 ---")
reset_ip_guard_state()
db = fresh_db()
banned_at = None
for i in range(1, 25):
    just_banned = ip_guard.record_failure(db, "5.6.7.8", "POST", "/api/bot/status", had_credential=True)
    if just_banned and banned_at is None:
        banned_at = i
check("bans on the 20th POST, not the 10th (it has SOME credential, so the "
      "looser POST-only rule applies, not the tight unknown-caller one)",
      banned_at, ip_guard.POST_BAD_CREDENTIAL_LIMIT)

print("\n--- a bad credential on GET (not POST) is never auto-banned ---")
reset_ip_guard_state()
db = fresh_db()
for _ in range(100):
    ip_guard.record_failure(db, "9.9.9.9", "GET", "/api/bot/status", had_credential=True)
check("100 GETs with a bad token never bans - only POST counts for this rule",
      ip_guard.is_banned("9.9.9.9"), False)

print("\n--- /api/auth/login is exempt (it has its own dedicated rate limiter) ---")
reset_ip_guard_state()
db = fresh_db()
for _ in range(50):
    ip_guard.record_failure(db, "10.10.10.10", "POST", "/api/auth/login", had_credential=False)
check("50 failed logins from one IP never trips this module's ban",
      ip_guard.is_banned("10.10.10.10"), False)
check("...so a real admin mistyping their password can't get permanently locked out here",
      db.query(models.IpBan).filter(models.IpBan.ip == "10.10.10.10").first(), None)

print("\n--- RADIUS: an unknown username (کاربر ناشناس) bans at exactly 10 ---")
reset_ip_guard_state()
db = fresh_db()
banned_at = None
for i in range(1, 15):
    just_banned = ip_guard.record_radius_reject(db, "37.114.246.98", "unknown_user")
    if just_banned and banned_at is None:
        banned_at = i
check("bans on the 10th RADIUS attempt with an unknown username",
      banned_at, ip_guard.RADIUS_UNKNOWN_LIMIT)
check("is_banned() reflects it", ip_guard.is_banned("37.114.246.98"), True)

print("\n--- RADIUS: a disabled connection (اتصال غیرفعال) bans at exactly 20, not 10 ---")
reset_ip_guard_state()
db = fresh_db()
banned_at = None
for i in range(1, 25):
    just_banned = ip_guard.record_radius_reject(db, "79.127.122.174", "disabled")
    if just_banned and banned_at is None:
        banned_at = i
check("bans on the 20th, matching the looser threshold for a real "
      "(if inactive) account rather than a blind prober",
      banned_at, ip_guard.RADIUS_INACTIVE_LIMIT)

print("\n--- RADIUS: other reject reasons (wrong password, quota, expiry) never auto-ban ---")
reset_ip_guard_state()
db = fresh_db()
for kind in ("auth_fail", "quota_exceeded", "expired"):
    for _ in range(50):
        ip_guard.record_radius_reject(db, "8.8.8.8", kind)
check("a real customer mistyping a password, or hitting their own quota/expiry, "
      "never gets their IP banned by this", ip_guard.is_banned("8.8.8.8"), False)

print("\n--- the sliding window: old hits age out and don't count forever ---")
reset_ip_guard_state()
db = fresh_db()
now = dt.datetime.utcnow()
# 9 hits from 20 minutes ago (outside the 10-minute window) + fresh ones.
ip_guard._unknown_hits["7.7.7.7"] = [now - dt.timedelta(minutes=20) for _ in range(9)]
for i in range(1, 10):
    ip_guard.record_failure(db, "7.7.7.7", "GET", "/api/x", had_credential=False)
check("the 9 stale hits were pruned, so 9 fresh ones alone don't ban yet",
      ip_guard.is_banned("7.7.7.7"), False)
ip_guard.record_failure(db, "7.7.7.7", "GET", "/api/x", had_credential=False)
check("the 10th fresh one does", ip_guard.is_banned("7.7.7.7"), True)

print("\n--- unban: removes the row AND clears the in-memory block ---")
reset_ip_guard_state()
db = fresh_db()
ip_guard.ban(db, "1.1.1.1", reason="test", manual=True)
check("banned", ip_guard.is_banned("1.1.1.1"), True)
check("unban() reports success", ip_guard.unban(db, "1.1.1.1"), True)
check("no longer banned", ip_guard.is_banned("1.1.1.1"), False)
check("row is gone", db.query(models.IpBan).filter(models.IpBan.ip == "1.1.1.1").first(), None)
check("unbanning something not banned reports False, doesn't raise",
      ip_guard.unban(db, "not-there"), False)

print("\n--- manual ban stays flagged manual even if auto-ban logic later touches it ---")
reset_ip_guard_state()
db = fresh_db()
ip_guard.ban(db, "2.2.2.2", reason="known bad actor", manual=True)
ip_guard.ban(db, "2.2.2.2", reason="also auto-tripped", manual=False)
row = db.query(models.IpBan).filter(models.IpBan.ip == "2.2.2.2").first()
check("a manual ban is never demoted back to auto by a later trip", row.is_manual, True)

print("\n--- refresh_from_db: a restart doesn't quietly un-ban everyone ---")
reset_ip_guard_state()
db = fresh_db()
db.add(models.IpBan(ip="3.3.3.3", reason="pre-existing", hit_count=10, is_manual=False))
db.commit()
check("nothing banned yet in this fresh process", ip_guard.is_banned("3.3.3.3"), False)
ip_guard.refresh_from_db(db)
check("...until refresh_from_db loads the persisted list at startup", ip_guard.is_banned("3.3.3.3"), True)

print("\n--- list_bans: newest first ---")
reset_ip_guard_state()
db = fresh_db()
ip_guard.ban(db, "a.a.a.a", reason="first", manual=True)
ip_guard.ban(db, "b.b.b.b", reason="second", manual=True)
ips = [b.ip for b in ip_guard.list_bans(db)]
check("most recently banned appears first", ips[0], "b.b.b.b")

print("\n" + "=" * 60 + "\n--- the middleware itself, end to end ---")

import asyncio

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

reset_ip_guard_state()
# StaticPool - TestClient dispatches through anyio's worker thread, and a
# plain sqlite://  :memory: engine hands out a BRAND NEW, empty database on
# every new connection unless pinned to a single shared connection (the
# same class of bug fixed in license_server/store.py's make_engine()).
_mw_engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(_mw_engine)
_MwSession = sessionmaker(bind=_mw_engine)

mw_app = FastAPI()


@mw_app.middleware("http")
async def _guard(request, call_next):
    return await ip_guard.guard_request(request, call_next, _MwSession)


@mw_app.get("/api/protected")
def protected(authorization: str | None = Header(default=None)):
    if not authorization:
        raise HTTPException(401, "no auth")
    return {"ok": True}


@mw_app.post("/api/protected")
def protected_post(x_api_key: str | None = Header(default=None, alias="X-API-Key")):
    if not x_api_key or x_api_key != "good-key":
        raise HTTPException(401, "bad key")
    return {"ok": True}


@mw_app.post("/api/auth/login")
def fake_login():
    raise HTTPException(401, "wrong password")


mw_client = TestClient(mw_app)

print("--- an unauthenticated caller trips the ban after 10 GETs, then is refused outright ---")
last_status = None
for i in range(12):
    resp = mw_client.get("/api/protected", headers={"X-Real-IP": "50.50.50.50"})
    last_status = resp.status_code
check("the 12th request (past the 10-trip threshold) is the flat ban response, "
      "not the endpoint's own 401", last_status, 403)
check("...and the endpoint's own logic never even ran for it "
      "(ban message, not 'no auth')", resp.json()["detail"], "دسترسی این آی‌پی به پنل مسدود شده است")

print("\n--- a DIFFERENT IP is unaffected ---")
resp = mw_client.get("/api/protected", headers={"X-Real-IP": "60.60.60.60"})
check("a fresh IP still gets the normal 401, not blocked", resp.status_code, 401)

print("\n--- /api/auth/login failures never ban, even hammered ---")
for _ in range(30):
    mw_client.post("/api/auth/login", headers={"X-Real-IP": "70.70.70.70"})
resp = mw_client.get("/api/protected", headers={"X-Real-IP": "70.70.70.70"})
check("still just a normal 401 on another endpoint - login attempts don't ban this IP",
      resp.status_code, 401)

print("\n--- OPTIONS (CORS preflight) is never blocked, even for an already-banned IP ---")
resp = mw_client.options("/api/protected", headers={"X-Real-IP": "50.50.50.50"})
check("OPTIONS passes through regardless of ban state", resp.status_code != 403, True)

print("\n" + "=" * 60)
print("--- REPRODUCTION: X-Real-IP is trusted with no notion of a trusted proxy ---")
print("(2026-09-27 audit finding - see the trusted-proxy design report; this")
print(" section only proves the CURRENT behaviour, it changes nothing yet)")
print("=" * 60)

# ip_guard.request_ip() (and routers/auth.py's near-identical _client_ip())
# read X-Real-IP unconditionally - there is no check on WHO the direct TCP
# peer (request.client.host) actually is. In production this header is
# meant to be set by nginx from $remote_addr, overwriting anything the
# client sent - but nothing in this module enforces that nginx (or Caddy,
# in the optional TLS profile) is really what is talking to it. Two
# concrete consequences, reproduced below:
#
#   1. If the backend is ever reachable without going through nginx first
#      (a misconfigured proxy, PANEL_API_BIND=0.0.0.0 for the HA/remote-
#      bridge features, or plain localhost during ops work), a caller can
#      set X-Real-IP to anything and ip_guard/the login limiter will act on
#      that fabricated address instead of the caller's real one.
#   2. Everything this module does is ip-KEYED, so a spoofable ip is not
#      just "unattributed" - it can frame an innocent third party (ban
#      their real address) or let an attacker dodge the ban outright by
#      rotating the header on every request.


def fake_request(headers: dict, client_host: str | None):
    """A minimal stand-in for Starlette's Request - only the two attributes
    ip_guard.request_ip()/auth.py's _client_ip() actually read."""
    class _Client:
        def __init__(self, host):
            self.host = host

    class _Headers(dict):
        def get(self, key, default=None):
            return super().get(key.lower(), default)

    class _Req:
        def __init__(self):
            self.headers = _Headers({k.lower(): v for k, v in headers.items()})
            self.client = _Client(client_host) if client_host else None

    return _Req()


print("\n--- ip_guard.request_ip(): a caller-supplied header is trusted with no peer check ---")
spoofed = fake_request({"X-Real-IP": "203.0.113.9"}, client_host="198.51.100.50")
check("the caller's own header wins over the real TCP peer - nothing here "
      "verifies 198.51.100.50 (the real peer) is a proxy this panel trusts",
      ip_guard.request_ip(spoofed), "203.0.113.9")

print("\n--- reproduction: rotating the header defeats the auto-ban entirely ---")
reset_ip_guard_state()
db = fresh_db()
for i in range(30):
    # A REAL attacker script would vary this per request; a fixed range is
    # enough to show none of them individually reaches the 10-hit threshold.
    ip_guard.record_failure(db, f"203.0.113.{i % 15}", "GET", "/api/users", had_credential=False)
check("30 unauthenticated hits, spread over a rotating spoofed header, ban NOTHING "
      "(each fake address alone stays under the 10-hit limit)",
      any(ip_guard.is_banned(f"203.0.113.{i % 15}") for i in range(30)), False)

print("\n--- reproduction: the SAME spoofing frames an innocent real address ---")
reset_ip_guard_state()
db = fresh_db()
for _ in range(10):
    # An attacker who ALREADY knows a victim's real IP (e.g. a shared
    # office/NAT address, or one leaked elsewhere) can put it in every
    # request's X-Real-IP instead of their own, and ip_guard bans the
    # victim's address, not the attacker's.
    ip_guard.record_failure(db, "198.51.100.77", "GET", "/api/users", had_credential=False)
check("the victim's address ends up banned, not the actual caller",
      ip_guard.is_banned("198.51.100.77"), True)

print("\n--- routers/auth.py's _client_ip() has the exact same trust problem ---")
from app.routers import auth as auth_router  # noqa: E402

spoofed2 = fake_request({"X-Real-IP": "203.0.113.66"}, client_host="198.51.100.50")
check("auth.py's own IP resolver also just believes the header",
      auth_router._client_ip(spoofed2), "203.0.113.66")

print("\n--- reproduction: the login brute-force limiter can be spoofed around too ---")
db = fresh_db()
now = dt.datetime.utcnow()
real_attacker_ip = "203.0.113.200"
for i in range(20):
    # Same rotation trick against LOGIN_RATE_LIMIT_MAX_FAILURES (10 failures
    # / 15 minutes per IP, routers/auth.py) - every failed attempt is logged
    # under a DIFFERENT spoofed address, so no single one ever crosses the
    # real threshold and the account is never actually protected.
    db.add(models.AdminLoginLog(
        attempted_username="root", ip_address=f"203.0.113.{100 + (i % 20)}",
        success=False, created_at=now,
    ))
db.commit()
check("20 failed attempts 'from' 20 different spoofed addresses - none of "
      "them individually trips the 10-failure lockout",
      any(auth_router._is_rate_limited(db, f"203.0.113.{100 + i}") for i in range(20)), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
