"""The licence server's web layer: one public endpoint + a vendor console.

Run locally:
    cd license_server
    LICENSE_ADMIN_PASSWORD='choose-one' python3 -m uvicorn app:app --port 9000

The public surface is exactly ONE route - POST /heartbeat - that customer
panels call. Everything else lives under /console and requires the operator
password. Kept minimal on purpose: this service exists to answer "is this
install still allowed?" and to let the vendor flip that answer. It is not a
second product.
"""
from __future__ import annotations

import os
import secrets

from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

import store

DB_URL = os.environ.get("LICENSE_DB_URL", "sqlite:////data/license.db")
# First-run console password. After first boot it is stored (hashed) and
# this is ignored - rotate from inside the console instead.
DEFAULT_ADMIN_PASSWORD = os.environ.get("LICENSE_ADMIN_PASSWORD", "")

app = FastAPI(title="NetCip License Server", docs_url=None, redoc_url=None, openapi_url=None)

_Session = store.make_session_factory(DB_URL)

# Session cookies for the console - kept in memory. The operator is one
# person on one browser; a restart just means logging in again.
_SESSIONS: set[str] = set()
COOKIE = "lic_console"


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


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


# --------------------------------------------------------------------------
# public: the heartbeat
# --------------------------------------------------------------------------
@app.post("/heartbeat")
async def heartbeat(request: Request, db=Depends(get_db)):
    """A customer panel checking in. Body is JSON:

        {"license_id": "...", "fingerprint": "...",
         "panel_version": "...", "customers": 123}

    Returns {"revoked": bool, "lock_scope": str}. Deliberately answers even
    for an unknown licence (by registering it) - a panel with a valid signed
    licence we have simply never seen should not be told "unknown" and lock;
    it self-registers as allowed and the operator sees it appear.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "expected JSON")
    license_id = (body or {}).get("license_id")
    if not license_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "license_id is required")

    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() \
        or (request.client.host if request.client else None)

    _install, response = store.record_heartbeat(
        db,
        license_id=str(license_id),
        fingerprint=(body.get("fingerprint") or None),
        ip=client_ip,
        panel_version=(body.get("panel_version") or None),
        reported_customers=body.get("customers"),
    )
    return response


# --------------------------------------------------------------------------
# console auth
# --------------------------------------------------------------------------
def require_console(request: Request):
    token = request.cookies.get(COOKIE)
    if not token or token not in _SESSIONS:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/console/login"})
    return True


@app.get("/console/login", response_class=HTMLResponse)
def login_form(error: str = ""):
    msg = f'<p style="color:#dc2626">{error}</p>' if error else ""
    return _page("ورود", f"""
      <form method="post" action="/console/login" style="max-width:320px;margin:15vh auto">
        <h2>کنسول لایسنس</h2>
        {msg}
        <input name="password" type="password" placeholder="رمز عبور" autofocus
               style="width:100%;padding:10px;margin:8px 0;box-sizing:border-box">
        <button style="width:100%;padding:10px">ورود</button>
      </form>
    """)


@app.post("/console/login")
def login(response: Response, db=Depends(get_db), password: str = Form(...)):
    if not store.check_admin_password(db, password):
        return RedirectResponse("/console/login?error=رمز اشتباه است", status_code=303)
    token = secrets.token_urlsafe(24)
    _SESSIONS.add(token)
    resp = RedirectResponse("/console", status_code=303)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="lax", max_age=86400)
    return resp


@app.get("/console/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    _SESSIONS.discard(token)
    resp = RedirectResponse("/console/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# --------------------------------------------------------------------------
# console: the list + the levers
# --------------------------------------------------------------------------
@app.get("/console", response_class=HTMLResponse)
def console(request: Request, db=Depends(get_db), _=Depends(require_console)):
    import datetime as dt
    now = dt.datetime.utcnow()
    rows = []
    for i in store.list_installs(db):
        silent = now - i.last_seen
        # A row that has not checked in for a while is worth seeing in
        # colour; one whose fingerprint changed is the real alarm.
        stale = silent.total_seconds() > 6 * 3600
        moved = i.fingerprint_changed_at is not None
        badge = ""
        if moved:
            badge = '<span style="background:#fee2e2;color:#b91c1c;padding:2px 6px;border-radius:6px">اثر انگشت عوض شد</span>'
        status_cell = ('<span style="color:#b91c1c">قفل‌شده</span>' if i.revoked
                       else '<span style="color:#059669">فعال</span>')
        rows.append(f"""
          <tr style="border-top:1px solid #eee{';background:#fff7ed' if stale else ''}">
            <td>{_esc(i.label) or '<i style=color:#999>بی‌نام</i>'}<br>
                <code style="font-size:11px;color:#666">{_esc(i.license_id)}</code></td>
            <td>{status_cell}</td>
            <td style="font-size:12px">{_ago(silent)} پیش<br>{_esc(i.last_ip) or ''}</td>
            <td style="font-size:12px">{_esc(i.panel_version) or '?'}<br>{i.reported_customers if i.reported_customers is not None else ''} کاربر</td>
            <td>{badge}</td>
            <td>
              <form method="post" action="/console/toggle" style="display:inline">
                <input type="hidden" name="license_id" value="{_esc(i.license_id)}">
                <input type="hidden" name="revoked" value="{'0' if i.revoked else '1'}">
                <button>{'باز کن' if i.revoked else 'قفل کن'}</button>
              </form>
              <form method="post" action="/console/scope" style="display:inline">
                <input type="hidden" name="license_id" value="{_esc(i.license_id)}">
                <select name="scope" onchange="this.form.submit()" style="font-size:12px">
                  {_scope_options(i.lock_scope)}
                </select>
              </form>
              <form method="post" action="/console/label" style="display:inline">
                <input type="hidden" name="license_id" value="{_esc(i.license_id)}">
                <input name="label" value="{_esc(i.label) or ''}" placeholder="نام" style="width:90px;font-size:12px">
                <button>ذخیره</button>
              </form>
            </td>
          </tr>
        """)
    body = f"""
      <div style="display:flex;justify-content:space-between;align-items:center">
        <h2>نصب‌های فعال ({len(rows)})</h2>
        <a href="/console/logout">خروج</a>
      </div>
      <table style="width:100%;border-collapse:collapse;font-size:14px">
        <tr style="text-align:right;color:#666">
          <th>مشتری</th><th>وضعیت</th><th>آخرین پینگ</th><th>نسخه</th><th>هشدار</th><th>کنترل</th>
        </tr>
        {''.join(rows) or '<tr><td colspan=6 style="padding:24px;color:#999">هنوز پنلی پینگ نزده است.</td></tr>'}
      </table>
    """
    return _page("کنسول لایسنس", body)


@app.post("/console/toggle")
def toggle(db=Depends(get_db), _=Depends(require_console),
           license_id: str = Form(...), revoked: str = Form(...)):
    store.set_revoked(db, license_id, revoked == "1")
    return RedirectResponse("/console", status_code=303)


@app.post("/console/scope")
def scope(db=Depends(get_db), _=Depends(require_console),
          license_id: str = Form(...), scope: str = Form(...)):
    try:
        store.set_lock_scope(db, license_id, scope)
    except ValueError:
        pass
    return RedirectResponse("/console", status_code=303)


@app.post("/console/label")
def label(db=Depends(get_db), _=Depends(require_console),
          license_id: str = Form(...), label: str = Form("")):
    store.set_label(db, license_id, label)
    return RedirectResponse("/console", status_code=303)


# --------------------------------------------------------------------------
# tiny HTML helpers - no template engine, this is a one-operator console
# --------------------------------------------------------------------------
def _esc(text) -> str:
    if not text:
        return ""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang=fa dir=rtl><head>
      <meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
      <title>{_esc(title)}</title>
      <style>body{{font-family:Tahoma,sans-serif;background:#f8fafc;color:#0f172a;margin:0;padding:24px}}
      button{{cursor:pointer;padding:4px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;margin:2px}}
      td{{padding:8px;vertical-align:top}}</style></head><body>{body}</body></html>""")


def _scope_options(current: str) -> str:
    labels = {
        store.SCOPE_PANEL_ONLY: "فقط پنل",
        store.SCOPE_PANEL_AND_BOT: "پنل + ربات",
        store.SCOPE_EVERYTHING: "همه‌چیز",
    }
    return "".join(
        f'<option value="{s}"{" selected" if s == current else ""}>{labels[s]}</option>'
        for s in store.LOCK_SCOPES
    )


def _ago(delta) -> str:
    secs = int(delta.total_seconds())
    if secs < 60:
        return "چند لحظه"
    if secs < 3600:
        return f"{secs // 60} دقیقه"
    if secs < 86400:
        return f"{secs // 3600} ساعت"
    return f"{secs // 86400} روز"
