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

# A second client whose ASGI-level peer is a TRUSTED address (inside the
# default Docker bridge range), simulating "arrived via nginx" - as opposed
# to mw_client above, whose peer is httpx/Starlette's own "testclient"
# placeholder, which is correctly UNTRUSTED (see the trusted-proxy section
# below). The four checks right below this are about the ban-COUNTING logic
# itself (does the 10th request trip it, does a different address stay
# clear, ...), not about spoofing, so they need a peer whose X-Real-IP is
# actually honoured - exactly like a real request that came through nginx.
#
# httpx.ASGITransport (which is what lets Starlette's own TestClient set a
# custom peer at all) only speaks async, so this wraps it in a one-shot
# event loop per call rather than pulling in Starlette's TestClient a
# second time just to override one constructor argument it doesn't expose.
import httpx  # noqa: E402


class _TrustedPeerClient:
    def __init__(self, app, peer_host: str):
        self._transport = httpx.ASGITransport(app=app, client=(peer_host, 1))

    def _request(self, method: str, url: str, **kw):
        async def _go():
            async with httpx.AsyncClient(transport=self._transport, base_url="http://testserver") as ac:
                return await ac.request(method, url, **kw)
        return asyncio.run(_go())

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def options(self, url, **kw):
        return self._request("OPTIONS", url, **kw)


trusted_mw_client = _TrustedPeerClient(mw_app, "172.20.0.9")

print("--- an unauthenticated caller trips the ban after 10 GETs, then is refused outright ---")
last_status = None
for i in range(12):
    resp = trusted_mw_client.get("/api/protected", headers={"X-Real-IP": "50.50.50.50"})
    last_status = resp.status_code
check("the 12th request (past the 10-trip threshold) is the flat ban response, "
      "not the endpoint's own 401", last_status, 403)
check("...and the endpoint's own logic never even ran for it "
      "(ban message, not 'no auth')", resp.json()["detail"], "دسترسی این آی‌پی به پنل مسدود شده است")

print("\n--- a DIFFERENT IP is unaffected ---")
resp = trusted_mw_client.get("/api/protected", headers={"X-Real-IP": "60.60.60.60"})
check("a fresh IP still gets the normal 401, not blocked", resp.status_code, 401)

print("\n--- /api/auth/login failures never ban, even hammered ---")
for _ in range(30):
    trusted_mw_client.post("/api/auth/login", headers={"X-Real-IP": "70.70.70.70"})
resp = trusted_mw_client.get("/api/protected", headers={"X-Real-IP": "70.70.70.70"})
check("still just a normal 401 on another endpoint - login attempts don't ban this IP",
      resp.status_code, 401)

print("\n--- OPTIONS (CORS preflight) is never blocked, even for an already-banned IP ---")
resp = trusted_mw_client.options("/api/protected", headers={"X-Real-IP": "50.50.50.50"})
check("OPTIONS passes through regardless of ban state", resp.status_code != 403, True)

print("\n" + "=" * 60)
print("--- TRUSTED-PROXY IP RESOLUTION (fixed 2026-09-27) ---")
print("=" * 60)

# services/ip_guard.resolve_client_ip() is now the ONE place both this
# module and routers/auth.py's login rate-limiter decide what IP a request
# is really from. X-Real-IP is honoured ONLY when request.client.host (the
# real, unspoofable TCP peer) is itself a trusted proxy - see
# DEFAULT_TRUSTED_PROXY_CIDRS. Everything below was, until this fix, exactly
# backwards: the header won regardless of who sent it, which let a caller
# either dodge the auto-ban/login-lockout by rotating a fake address, or
# frame an innocent real IP by reusing it. 198.51.100.0/24 and
# 203.0.113.0/24 (RFC 5737 TEST-NET ranges) stand in for "the internet" here
# - neither is in any trusted range, exactly like a real external caller.


def fake_request(headers: dict, client_host: str | None):
    """A minimal stand-in for Starlette's Request - only the two attributes
    resolve_client_ip()/auth.py's _client_ip() actually read."""
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


print("\n--- an UNTRUSTED peer's header is ignored outright, even if it looks valid ---")
untrusted = fake_request({"X-Real-IP": "203.0.113.9"}, client_host="198.51.100.50")
check("the real (untrusted) peer wins, NOT the caller-supplied header",
      ip_guard.resolve_client_ip(untrusted), "198.51.100.50")

print("\n--- a TRUSTED peer (nginx's own docker-network address) with a valid header: header wins ---")
trusted_valid = fake_request({"X-Real-IP": "203.0.113.9"}, client_host="172.20.0.5")
check("nginx (inside the default Docker bridge range) is trusted, so its "
      "X-Real-IP is used - unchanged from before this fix, for the normal deployment",
      ip_guard.resolve_client_ip(trusted_valid), "203.0.113.9")

print("\n--- loopback (an admin's own external reverse proxy, PANEL_WEB_BIND=127.0.0.1) is trusted too ---")
trusted_loopback = fake_request({"X-Real-IP": "203.0.113.9"}, client_host="127.0.0.1")
check("127.0.0.1 is trusted by default", ip_guard.resolve_client_ip(trusted_loopback), "203.0.113.9")

print("\n--- a TRUSTED peer with an INVALID header falls back to the peer, not the header ---")
for bad_header in ["not-an-ip", "", "   ", "999.999.999.999"]:
    req = fake_request({"X-Real-IP": bad_header}, client_host="172.20.0.5")
    check(f"invalid header {bad_header!r} from a trusted peer falls back to the peer itself",
          ip_guard.resolve_client_ip(req), "172.20.0.5")

print("\n--- a TRUSTED peer with a COMMA-SEPARATED header falls back to the peer, not either address ---")
comma_req = fake_request({"X-Real-IP": "203.0.113.9, 198.51.100.1"}, client_host="172.20.0.5")
check("a real trusted proxy only ever sends ONE address - a list is treated as invalid, not "
      "'take the first one' (which would let a client behind that proxy smuggle a second value)",
      ip_guard.resolve_client_ip(comma_req), "172.20.0.5")

print("\n--- no peer at all (no header either) resolves to None, not a crash ---")
no_peer = fake_request({}, client_host=None)
check("no client, no header -> None", ip_guard.resolve_client_ip(no_peer), None)

print("\n--- no peer at all, but a header present: still None - a header alone proves nothing ---")
no_peer_spoofed = fake_request({"X-Real-IP": "203.0.113.9"}, client_host=None)
check("an absent peer is never 'trusted', regardless of any header",
      ip_guard.resolve_client_ip(no_peer_spoofed), None)

print("\n--- IPv6: a trusted IPv6 peer (::1) with a valid IPv6 header ---")
v6_trusted = fake_request({"X-Real-IP": "2001:db8::1"}, client_host="::1")
check("loopback works in IPv6 form too", ip_guard.resolve_client_ip(v6_trusted), "2001:db8::1")

print("\n--- IPv6: an untrusted IPv6 peer's header is ignored, same as IPv4 ---")
v6_untrusted = fake_request({"X-Real-IP": "2001:db8::1"}, client_host="2001:db8::dead")
check("an arbitrary public IPv6 peer is not trusted by default",
      ip_guard.resolve_client_ip(v6_untrusted), "2001:db8::dead")

print("\n--- TRUSTED_PROXY_CIDRS is overridable, and a garbage entry in it is skipped, not fatal ---")
os.environ["TRUSTED_PROXY_CIDRS"] = "203.0.113.0/24, not-a-cidr-at-all"
try:
    custom_nets = ip_guard._parse_trusted_proxy_cidrs()
    check("the valid entry parsed", any(str(n) == "203.0.113.0/24" for n in custom_nets), True)
    check("the garbage entry was dropped, not raised", len(custom_nets), 1)
finally:
    os.environ.pop("TRUSTED_PROXY_CIDRS", None)

print("\n--- routers/auth.py's _client_ip() is not a second implementation - it's the SAME resolver ---")
from app.routers import auth as auth_router  # noqa: E402

check("auth.py imports the exact ip_guard module this file tests against "
      "(not a copy, not a re-import under a different name)",
      auth_router.ip_guard is ip_guard, True)
for headers, client_host in [
    ({"X-Real-IP": "203.0.113.9"}, "198.51.100.50"),   # untrusted peer, spoofed header
    ({"X-Real-IP": "203.0.113.9"}, "172.20.0.5"),      # trusted peer, valid header
    ({"X-Real-IP": "garbage"}, "172.20.0.5"),          # trusted peer, invalid header
    ({}, "203.0.113.1"),                                # no header at all
]:
    req_a = fake_request(headers, client_host)
    req_b = fake_request(headers, client_host)
    check(f"_client_ip and resolve_client_ip agree for headers={headers!r} peer={client_host!r}",
          auth_router._client_ip(req_a), ip_guard.resolve_client_ip(req_b))

print("\n--- the middleware itself: a spoofed header can no longer frame an innocent real IP ---")
reset_ip_guard_state()
victim_ip = "198.51.100.77"
for _ in range(15):
    # TestClient's own peer ("testclient", not a real IP) is untrusted, so
    # everything below is now attributed to THAT, no matter what X-Real-IP
    # claims - the header is not even a well-formed spoof target here, which
    # is the point: an attacker's real peer is never one of the trusted
    # ranges either.
    mw_client.get("/api/protected", headers={"X-Real-IP": victim_ip})
check("the innocent victim IP was never actually banned (the middleware's own "
      "TestClient peer, not X-Real-IP, is what gets attributed and counted)",
      ip_guard.is_banned(victim_ip), False)

print("\n--- login rate-limiter: rotating X-Real-IP from an untrusted peer no longer hides anything ---")
db = fresh_db()
now = dt.datetime.utcnow()
real_peer = "203.0.113.201"
for i in range(12):
    req = fake_request({"X-Real-IP": f"203.0.113.{50 + i}"}, client_host=real_peer)
    resolved = auth_router._client_ip(req)
    db.add(models.AdminLoginLog(
        attempted_username="root", ip_address=resolved, success=False, created_at=now,
    ))
db.commit()
check("every rotated-header attempt still resolved to the ONE real peer, so it "
      "correctly crosses the 10-failure lockout",
      auth_router._is_rate_limited(db, real_peer), True)
check("none of the fabricated addresses accumulated any failures of their own",
      auth_router._is_rate_limited(db, "203.0.113.50"), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
