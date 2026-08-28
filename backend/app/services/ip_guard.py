"""Auto-bans an IP address that keeps hammering the API without valid
credentials - "یه ای پی خاصی که همش داره ریکوست میزنه و کاربر ناشناس هست
رو بعد از ۱۰ ریکوست بن کنه ... یا اگر اتصال غیرفعال داره بعد از ۲۰ ریکوست
پست هم اونم بره تو بن لیست" (an IP that keeps making requests as an unknown
user gets banned after 10 requests; one presenting an inactive/invalid
credential on POST requests gets banned after 20).

Two separate counters, on purpose - they catch two different things:

  - UNKNOWN: a request to a protected endpoint carrying NO credential at
    all (no Authorization header, no X-API-Key header). This is a blind
    prober/scanner - nobody legitimate calls a protected endpoint with
    nothing to identify themselves. Tight limit (10), any HTTP method.

  - POST_BAD_CREDENTIAL: a POST request that DID present a credential, but
    it was rejected (expired token, revoked/disabled API key, wrong
    signature). This is looser (20) and POST-only on purpose - it is where
    a compromised/leaked key or a misconfigured integration shows up as
    repeated write attempts, and a genuine client can plausibly retry a
    handful of times (a token that just expired, a key rotated moments
    ago) before it's clearly abuse rather than bad timing.

`/api/auth/login` is deliberately excluded from both counters: it already
has its own dedicated brute-force guard (routers/auth.py's
LOGIN_RATE_LIMIT_*, a temporary 15-minute 429 keyed off AdminLoginLog) built
around the fact that a real admin might genuinely mistype their password a
few times. Layering this module's PERMANENT ban on top of that would let a
few fat-fingered login attempts get an admin's own office IP locked out of
the panel forever. A banned IP is still refused at login (and everywhere
else) - it just cannot get banned FOR failing to log in.

State is kept in-process (a dict of sliding-window timestamps per IP, plus
an in-memory set mirroring the `ip_bans` table) rather than hitting the
database on every single request - this runs on the hot path of every API
call. The ban itself IS persisted (survives a restart/redeploy); the
sliding-window counters that lead up to a ban do not need to (a restart
naturally resets an in-progress count back to zero, which is an acceptable
trade for not writing to the database on every failed request).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from sqlalchemy.orm import Session

from .. import models

logger = logging.getLogger("ip_guard")

UNKNOWN_WINDOW = dt.timedelta(minutes=10)
UNKNOWN_LIMIT = 10

POST_BAD_CREDENTIAL_WINDOW = dt.timedelta(minutes=10)
POST_BAD_CREDENTIAL_LIMIT = 20

EXEMPT_PATHS = {"/api/auth/login", "/api/health"}

RADIUS_UNKNOWN_WINDOW = dt.timedelta(minutes=10)
RADIUS_UNKNOWN_LIMIT = 10

RADIUS_INACTIVE_WINDOW = dt.timedelta(minutes=10)
RADIUS_INACTIVE_LIMIT = 20

_lock = threading.Lock()
_banned_ips: set[str] = set()
_banned_loaded = False
_unknown_hits: dict[str, list[dt.datetime]] = {}
_post_bad_cred_hits: dict[str, list[dt.datetime]] = {}
_radius_unknown_hits: dict[str, list[dt.datetime]] = {}
_radius_inactive_hits: dict[str, list[dt.datetime]] = {}


def refresh_from_db(db: Session) -> None:
    """Loads the current ban list into memory - called once at startup so
    a redeploy doesn't quietly un-ban everyone until the next auto-trip."""
    global _banned_loaded
    with _lock:
        _banned_ips.clear()
        _banned_ips.update(ip for (ip,) in db.query(models.IpBan.ip).all())
        _banned_loaded = True
    logger.info("ip_guard: %d آی‌پی مسدود از قبل بارگذاری شد", len(_banned_ips))


def is_banned(ip: str | None) -> bool:
    if not ip:
        return False
    return ip in _banned_ips


def _prune(bucket: list[dt.datetime], cutoff: dt.datetime) -> list[dt.datetime]:
    return [t for t in bucket if t >= cutoff]


def _bump_and_maybe_ban(
    db: Session, ip: str, bucket_store: dict[str, list[dt.datetime]],
    window: dt.timedelta, limit: int, reason_fmt: str,
) -> bool:
    """Records one more hit for `ip` in `bucket_store`, prunes anything
    outside `window`, and bans once `limit` is reached. Shared by every
    counter this module keeps (HTTP-unknown, HTTP-POST-bad-credential,
    RADIUS-unknown-user, RADIUS-inactive-connection) - they differ only in
    which bucket/window/limit/wording they use."""
    now = dt.datetime.utcnow()
    with _lock:
        bucket = _prune(bucket_store.get(ip, []), now - window)
        bucket.append(now)
        bucket_store[ip] = bucket
        count = len(bucket)
    if count >= limit:
        ban(db, ip, reason=reason_fmt.format(count=count), hit_count=count)
        return True
    return False


def record_failure(db: Session, ip: str | None, method: str, path: str, had_credential: bool) -> bool:
    """Call once per HTTP request that came back 401/403. Returns True if
    this call is what just banned the IP (so the caller can log it once,
    loudly, rather than on every subsequent already-banned request)."""
    if not ip or path in EXEMPT_PATHS:
        return False

    if not had_credential:
        return _bump_and_maybe_ban(
            db, ip, _unknown_hits, UNKNOWN_WINDOW, UNKNOWN_LIMIT,
            "{count} درخواست بدون احراز هویت در ۱۰ دقیقه (احتمال اسکن خودکار)",
        )

    if method.upper() == "POST":
        return _bump_and_maybe_ban(
            db, ip, _post_bad_cred_hits, POST_BAD_CREDENTIAL_WINDOW, POST_BAD_CREDENTIAL_LIMIT,
            "{count} درخواست POST با اعتبار نامعتبر/غیرفعال در ۱۰ دقیقه",
        )

    # A non-POST request WITH a credential that was still rejected (e.g. a
    # GET with an expired token) is not auto-banned - too easy to be a
    # session that simply timed out normally.
    return False


def record_radius_reject(db: Session, ip: str | None, reject_kind: str | None) -> bool:
    """Call once per REAL RADIUS Access-Request rejection (see
    services/radius_server.py's HandleAuthPacket) - deliberately BEFORE
    that function's own `_should_log_rejection` de-dup gate, not after: that
    gate exists to keep the admin-facing لاگ رادیوس page from filling with
    duplicate rows for the same repeated failure, and only lets one row
    through per (kind, username, ip) every 10 minutes. Counting bans off
    the de-duplicated rate would mean a script hammering the NAS every few
    seconds still only ever contributes ONE tick per 10 minutes here -
    the ban would functionally never trip. So this sees every attempt,
    the log page sees the deduplicated summary of them.

    Only two of radius_server's reject_kind values are covered, matching
    what was asked for - "کاربر ناشناس" (a username with no matching
    Connection at all - a blind prober, not a customer) and "اتصال
    غیرفعال" (a real customer's connection, but administratively
    disabled/expired) - both to hit a limit here, unlike auth_fail
    (wrong password on a REAL active account, which happens to real
    customers who mistype something) or quota_exceeded/expired (an
    account's own service state, not IP abuse)."""
    if not ip or not reject_kind:
        return False
    if reject_kind == "unknown_user":
        return _bump_and_maybe_ban(
            db, ip, _radius_unknown_hits, RADIUS_UNKNOWN_WINDOW, RADIUS_UNKNOWN_LIMIT,
            "{count} تلاش RADIUS با نام کاربری ناشناس در ۱۰ دقیقه",
        )
    if reject_kind == "disabled":
        return _bump_and_maybe_ban(
            db, ip, _radius_inactive_hits, RADIUS_INACTIVE_WINDOW, RADIUS_INACTIVE_LIMIT,
            "{count} تلاش RADIUS به یک اتصال غیرفعال در ۱۰ دقیقه",
        )
    return False


def ban(db: Session, ip: str, reason: str, *, hit_count: int = 0, manual: bool = False) -> None:
    existing = db.query(models.IpBan).filter(models.IpBan.ip == ip).first()
    if existing:
        existing.reason = reason
        existing.hit_count = hit_count or existing.hit_count
        existing.is_manual = existing.is_manual or manual
        existing.banned_at = dt.datetime.utcnow()
    else:
        db.add(models.IpBan(ip=ip, reason=reason, hit_count=hit_count, is_manual=manual))
    db.commit()
    with _lock:
        _banned_ips.add(ip)
        _unknown_hits.pop(ip, None)
        _post_bad_cred_hits.pop(ip, None)
    logger.warning("ip_guard: آی‌پی %s مسدود شد - %s", ip, reason)


def unban(db: Session, ip: str) -> bool:
    row = db.query(models.IpBan).filter(models.IpBan.ip == ip).first()
    if not row:
        return False
    db.delete(row)
    db.commit()
    with _lock:
        _banned_ips.discard(ip)
        _unknown_hits.pop(ip, None)
        _post_bad_cred_hits.pop(ip, None)
    logger.info("ip_guard: مسدودیت آی‌پی %s برداشته شد", ip)
    return True


def list_bans(db: Session) -> list[models.IpBan]:
    return db.query(models.IpBan).order_by(models.IpBan.banned_at.desc()).all()


def request_ip(request) -> str | None:
    # Same convention as routers/auth.py's _client_ip: nginx.conf sets
    # X-Real-IP to $remote_addr, so this is the true client address even
    # though the backend container only ever sees nginx's own IP as
    # request.client.host.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else None)


async def guard_request(request, call_next, session_factory):
    """The actual middleware body - lives here (not inline in main.py) so it
    can be exercised directly in tests without booting the full app (which
    starts the scheduler/RADIUS server/telegram bot on FastAPI's startup
    event). main.py's `@app.middleware("http")` is a two-line wrapper around
    this.

    `session_factory` is whatever produces a fresh SQLAlchemy Session
    (SessionLocal in production, a test engine's sessionmaker in tests) -
    kept as a parameter rather than importing SessionLocal here to avoid
    this module reaching back into database.py/main.py's wiring.
    """
    if request.method == "OPTIONS":
        return await call_next(request)

    ip = request_ip(request)
    if ip and is_banned(ip):
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "دسترسی این آی‌پی به پنل مسدود شده است"}, status_code=403)

    response = await call_next(request)

    if ip and response.status_code in (401, 403) and request.url.path.startswith("/api/"):
        had_credential = bool(
            (request.headers.get("authorization") or "").strip()
            or (request.headers.get("x-api-key") or "").strip()
        )
        db = session_factory()
        try:
            just_banned = record_failure(db, ip, request.method, request.url.path, had_credential)
        finally:
            db.close()
        if just_banned:
            logger.warning("ip_guard: آی‌پی %s به‌صورت خودکار مسدود شد", ip)

    return response
