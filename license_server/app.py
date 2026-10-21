"""The licence server's web layer: one public endpoint + a vendor console.

Run locally:
    cd license_server
    LICENSE_ADMIN_PASSWORD='choose-one' python3 -m uvicorn app:app --port 9000

The public surface is exactly ONE route - POST /heartbeat - that customer
panels call. Its request and reply are a contract with every deployed panel
and are unchanged here. Everything else lives under /console and requires
the operator password: a session cookie (console_auth), a CSRF token on
every change, and a confirmation step before anything that locks a panel or
signs a licence. Every change is recorded (ledger.log_event).
"""
from __future__ import annotations

import datetime as dt
import os
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import update

import console_auth as auth
import issue_routes
from console_deps import (COOKIE, Ctx, _Session, _https, _same_origin, changing_ctx, client_ip, console_ctx, get_db, go,
                          install_or_404, row_for)
import detail_views
import ledger
import signing
import store
import views
from ui import SCOPE_LABELS, notice, page

# First-run console password. After first boot it is stored (hashed) and
# this is ignored - rotate from inside the console instead (/console/password).
DEFAULT_ADMIN_PASSWORD = os.environ.get("LICENSE_ADMIN_PASSWORD", "")
STATIC = Path(__file__).resolve().parent / "static"

app = FastAPI(title="NetCip License Server", docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
       "form-action 'self'; frame-ancestors 'none'; base-uri 'none'")


@app.on_event("startup")
def _startup():
    db = _Session()
    try:
        if DEFAULT_ADMIN_PASSWORD:
            result = store.ensure_admin_password(db, DEFAULT_ADMIN_PASSWORD)
            if result != "(existing)":
                print("license-server: console password set from LICENSE_ADMIN_PASSWORD")
        elif not store._get_setting(db, "admin_password_hash"):
            # No password set and none provided: generate one and print it
            # ONCE, so a fresh deploy is reachable but never passwordless.
            generated = secrets.token_urlsafe(12)
            store.ensure_admin_password(db, generated)
            print("=" * 60)
            print(f"license-server console password (save this): {generated}")
            print("=" * 60)
    finally:
        db.close()


@app.middleware("http")
async def _headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/console"):
        response.headers["Content-Security-Policy"] = CSP
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # pages carry tokens and operator data; never keep them in a cache
        response.headers["Cache-Control"] = "no-store"
    return response


# --------------------------------------------------------------------------
# public: the heartbeat (contract unchanged)
# --------------------------------------------------------------------------
@app.post("/heartbeat")
async def heartbeat(request: Request, db=Depends(get_db)):
    """A customer panel checking in. Body is JSON:

        {"license_id": "...", "fingerprint": "...",
         "panel_version": "...", "customers": 123, "activated_at": "..."}

    Returns {"revoked": bool, "lock_scope": str, "activated_at": str|None}.
    Deliberately answers even for an unknown licence (by registering it) - a
    panel with a valid signed licence we have simply never seen should not be
    told "unknown" and lock; it self-registers as allowed and the operator
    sees it appear.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected JSON")
    if not isinstance(body, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected a JSON object")
    license_id = body.get("license_id")
    if not license_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "license_id is required")

    activated_at = None
    raw_activated = (body.get("activated_at") or "").strip() if isinstance(body.get("activated_at"), str) else None
    if raw_activated:
        try:
            activated_at = dt.datetime.fromisoformat(raw_activated)
        except ValueError:
            activated_at = None  # a malformed date is ignored, never fatal

    customers = body.get("customers")
    _install, response = store.record_heartbeat(
        db,
        license_id=str(license_id)[:64],
        fingerprint=(str(body.get("fingerprint"))[:64] if body.get("fingerprint") else None),
        ip=client_ip(request),
        panel_version=(str(body.get("panel_version"))[:64] if body.get("panel_version") else None),
        reported_customers=customers if isinstance(customers, int) and not isinstance(customers, bool) else None,
        activated_at=activated_at,
    )
    return response


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_303_SEE_OTHER or not request.url.path.startswith("/console"):
        from fastapi.exception_handlers import http_exception_handler
        return await http_exception_handler(request, exc)
    token = request.cookies.get(COOKIE)
    session = auth.get(token)
    back = '<p><a class="btn" href="/console">بازگشت به فهرست</a></p>'
    return page("خطا", notice(str(exc.detail) if isinstance(exc.detail, str) else "درخواست انجام نشد.", "danger")
                + back, csrf=session.csrf if session else None, status_code=exc.status_code)


@app.get("/console/static/{name}")
def static_file(name: str):
    if name not in ("console.css", "console.js"):
        raise HTTPException(status.HTTP_404_NOT_FOUND)
    return FileResponse(STATIC / name)


# --------------------------------------------------------------------------
# login / logout / password
# --------------------------------------------------------------------------
@app.get("/console/login")
def login_form(error: str = ""):
    return views.login_page(error)


@app.post("/console/login")
def login(request: Request, db=Depends(get_db), password: str = Form("")):
    ip = client_ip(request) or "?"
    if not _same_origin(request):
        return go("/console/login?error=origin")
    if auth.login_blocked(ip):
        return go("/console/login?error=blocked")
    if not password or not store.check_admin_password(db, password):
        auth.login_failed(ip)
        ledger.log_event(db, "login_failed", ip=ip)
        return go("/console/login?error=wrong")
    auth.login_succeeded(ip)
    token, _session = auth.create()
    ledger.log_event(db, "login", ip=ip, session=ledger.session_tag(token))
    resp = go("/console")
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=_https(request),
                    path="/console", max_age=auth.ABSOLUTE_SECONDS)
    return resp


@app.get("/console/logout")
def logout_get():
    # logging out changes state, so it is a POST (from the header button)
    return go("/console")


@app.post("/console/logout")
def logout(db=Depends(get_db), ctx: Ctx = Depends(changing_ctx)):
    auth.drop(ctx.token)
    ledger.log_event(db, "logout", ip=ctx.ip, session=ctx.tag)
    resp = go("/console/login")
    resp.delete_cookie(COOKIE, path="/console")
    return resp


@app.get("/console/password")
def password_form(done: str = "", ctx: Ctx = Depends(console_ctx)):
    return views.password_page(ctx.csrf, done=done)


@app.post("/console/password")
def password_change(request: Request, db=Depends(get_db), ctx: Ctx = Depends(changing_ctx),
                    current: str = Form(""), new: str = Form(""), repeat: str = Form("")):
    if not store.check_admin_password(db, current):
        ledger.log_event(db, "login_failed", ip=ctx.ip, session=ctx.tag, detail={"where": "password_change"})
        return views.password_page(ctx.csrf, error="current")
    if len(new) < auth.MIN_PASSWORD_LENGTH:
        return views.password_page(ctx.csrf, error="short")
    if new != repeat:
        return views.password_page(ctx.csrf, error="mismatch")
    if new == current:
        return views.password_page(ctx.csrf, error="same")
    store.set_admin_password(db, new)
    ledger.log_event(db, "password_changed", ip=ctx.ip, session=ctx.tag)
    auth.drop_all()
    token, _session = auth.create()
    resp = go("/console/password?done=password")
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=_https(request),
                    path="/console", max_age=auth.ABSOLUTE_SECONDS)
    return resp


# --------------------------------------------------------------------------
# list, details, events
# --------------------------------------------------------------------------
@app.get("/console")
def console(db=Depends(get_db), ctx: Ctx = Depends(console_ctx), q: str = "", access: str = "",
            valid: str = "", contact: str = "", moved: str = "", done: str = ""):
    rows = ledger.overview(db)
    flag = moved == "1"
    if contact == "quiet":
        shown = [r for r in rows if ledger.matches(r, q=q, access=access, valid=valid, moved=flag)
                 and r["contact"]["state"] in ("silent", "never")]
    else:
        shown = [r for r in rows if ledger.matches(r, q=q, access=access, contact_state=contact, valid=valid,
                                                   moved=flag)]
    # current licences first; ones a reported-in successor replaced go last
    shown.sort(key=lambda r: r["replaced"])
    return views.list_page(shown, ledger.counts(rows), {"q": q, "access": access, "valid": valid, "contact": contact},
                           ctx.csrf, done)


@app.get("/console/license")
def license_detail(id: str = "", done: str = "", db=Depends(get_db), ctx: Ctx = Depends(console_ctx)):
    install = install_or_404(db, id, ctx)
    return detail_views.detail_page(db, row_for(db, install), ctx.csrf, done)


@app.get("/console/events")
def events(db=Depends(get_db), ctx: Ctx = Depends(console_ctx)):
    return views.events_page(ledger.events(db, limit=500), ctx.csrf)


# --------------------------------------------------------------------------
# access: lock / unlock / scope - always through a confirmation page
# --------------------------------------------------------------------------
@app.get("/console/license/access")
def access_confirm(id: str = "", revoked: str = "", scope: str = "", db=Depends(get_db),
                   ctx: Ctx = Depends(console_ctx)):
    install = install_or_404(db, id, ctx)
    if revoked not in ("0", "1") or scope not in SCOPE_LABELS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "وضعیت یا دامنهٔ قفل نامعتبر است.")
    if (revoked == "1") == bool(install.revoked) and scope == install.lock_scope:
        return go(views.link(install.license_id) + "&done=nochange")
    return detail_views.access_confirm_page(install, revoked == "1", scope, ctx.csrf)


@app.post("/console/license/access")
def access_apply(db=Depends(get_db), ctx: Ctx = Depends(changing_ctx), license_id: str = Form(""),
                 revoked: str = Form(""), scope: str = Form(""), expect_revoked: str = Form(""),
                 expect_scope: str = Form(""), reason: str = Form(""), ack: str = Form("")):
    install = install_or_404(db, license_id, ctx)
    if revoked not in ("0", "1") or scope not in SCOPE_LABELS or expect_revoked not in ("0", "1"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "وضعیت یا دامنهٔ قفل نامعتبر است.")
    want_revoked = revoked == "1"
    if (bool(install.revoked), install.lock_scope) != (expect_revoked == "1", expect_scope):
        return detail_views.access_confirm_page(install, want_revoked, scope, ctx.csrf, error="stale")
    if want_revoked and scope == store.SCOPE_EVERYTHING and ack != "yes":
        return detail_views.access_confirm_page(install, want_revoked, scope, ctx.csrf, error="ack")
    before = {"revoked": bool(install.revoked), "lock_scope": install.lock_scope}
    if before == {"revoked": want_revoked, "lock_scope": scope}:
        return go(views.link(license_id) + "&done=nochange")
    # Compare-and-set: applies only if nobody changed it since the
    # confirmation page was shown (another tab, a resend).
    result = db.execute(update(store.Install)
                        .where(store.Install.license_id == license_id,
                               store.Install.revoked == (expect_revoked == "1"),
                               store.Install.lock_scope == expect_scope)
                        .values(revoked=want_revoked, lock_scope=scope))
    if result.rowcount != 1:
        db.rollback()
        db.expire_all()
        return detail_views.access_confirm_page(install_or_404(db, license_id, ctx), want_revoked, scope,
                                                ctx.csrf, error="stale")
    ledger.log_event(db, "access_changed", license_id=license_id,
                     detail={"before": before, "after": {"revoked": want_revoked, "lock_scope": scope}},
                     reason=reason, ip=ctx.ip, session=ctx.tag, commit=False)
    db.commit()
    return go(views.link(license_id) + "&done=access")


@app.post("/console/license/label")
def label(db=Depends(get_db), ctx: Ctx = Depends(changing_ctx), license_id: str = Form(""),
          label: str = Form(""), note: str = Form("")):
    install = install_or_404(db, license_id, ctx)
    before = {"label": install.label, "note": install.note}
    after = {"label": label.strip()[:200] or None, "note": note.strip()[:2000] or None}
    if before != after:
        install.label, install.note = after["label"], after["note"]
        ledger.log_event(db, "label_changed", license_id=license_id, detail={"before": before, "after": after},
                         ip=ctx.ip, session=ctx.tag, commit=False)
        db.commit()
    return go(views.link(license_id) + "&done=" + ("label" if before != after else "nochange"))


@app.post("/console/license/import")
def import_token(db=Depends(get_db), ctx: Ctx = Depends(changing_ctx), license_id: str = Form(""),
                 token: str = Form("")):
    install = install_or_404(db, license_id, ctx)
    try:
        payload = signing.read_token(token)
    except signing.SigningKeyMissing:
        raise HTTPException(status.HTTP_409_CONFLICT, "کلید امضا روی این سرور نیست؛ امضای توکن قابل بررسی نیست.")
    except signing.TokenRejected as exc:
        message = ("این توکن با کلید امضای این سرور امضا نشده است." if str(exc) == "signature"
                   else "توکن خوانده نشد؛ متن کامل را دوباره کپی کنید.")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, message)
    if payload["id"] != install.license_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "این توکن متعلق به لایسنس دیگری است.")
    if ledger.get_issued(db, install.license_id) is not None:
        return go(views.link(license_id) + "&done=nochange")
    ledger.record_issued(db, payload, source="imported", commit=False)
    ledger.log_event(db, "imported", license_id=license_id,
                     detail={"expires_at": payload.get("exp"), "valid_days": payload.get("vd"),
                             "bound": bool(payload.get("fp"))}, ip=ctx.ip, session=ctx.tag, commit=False)
    db.commit()
    return go(views.link(license_id) + "&done=imported")


@app.get("/console/license/forget")
def forget_confirm(id: str = "", db=Depends(get_db), ctx: Ctx = Depends(console_ctx)):
    install = install_or_404(db, id, ctx)
    return detail_views.forget_page(install, ctx.csrf, blocked=bool(install.revoked))


@app.post("/console/license/forget")
def forget(db=Depends(get_db), ctx: Ctx = Depends(changing_ctx), license_id: str = Form("")):
    """Removes a row from the list (store.forget_install). Not a ban: a
    panel that heartbeats again re-registers, unnamed and OPEN - which is
    why a locked row cannot be removed: that would quietly unlock it."""
    install = install_or_404(db, license_id, ctx)
    if install.revoked:
        return detail_views.forget_page(install, ctx.csrf, blocked=True)
    snapshot = {"label": install.label, "lock_scope": install.lock_scope, "fingerprint": install.fingerprint,
                "heartbeat_count": install.heartbeat_count}
    store.forget_install(db, license_id)
    ledger.log_event(db, "forgotten", license_id=license_id, detail={"before": snapshot, "after": {}},
                     ip=ctx.ip, session=ctx.tag)
    return go("/console?done=forgotten")


app.include_router(issue_routes.router)
