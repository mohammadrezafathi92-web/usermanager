"""Shared console plumbing: the database session, the client address,
and the session/CSRF checks every console route depends on."""
from __future__ import annotations

import datetime as dt
import os
from typing import Optional

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

import console_auth as auth
import ledger
import store

DB_URL = os.environ.get("LICENSE_DB_URL", "sqlite:////data/license.db")
COOKIE = auth.COOKIE
_Session = store.make_session_factory(DB_URL)


def get_db():
    db = _Session()
    try:
        yield db
    finally:
        db.close()


def client_ip(request: Request) -> Optional[str]:
    # nginx sets X-Real-IP to the connecting address; X-Forwarded-For's
    # first entry is whatever the client chose to send.
    return (request.headers.get("x-real-ip", "").strip()
            or request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            or (request.client.host if request.client else None))


def _https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


class Ctx:
    def __init__(self, token: str, session: auth.Session, ip: Optional[str]):
        self.token, self.session, self.ip = token, session, ip
        self.csrf = session.csrf
        self.tag = ledger.session_tag(token)


def console_ctx(request: Request) -> Ctx:
    token = request.cookies.get(COOKIE)
    session = auth.get(token)
    if session is None:
        raise HTTPException(status.HTTP_303_SEE_OTHER,
                            headers={"Location": "/console/login" + ("?error=expired" if token else "")})
    return Ctx(token, session, client_ip(request))


def _same_origin(request: Request) -> bool:
    """A browser names the page a form was posted from. The console sends
    Referrer-Policy: same-origin, so a real console form carries its own
    origin; "null" (a sandboxed or privacy-stripped context) is refused
    like a foreign one. No Origin at all (a non-browser client) falls back
    to the CSRF token alone."""
    origin = request.headers.get("origin")
    if origin is None:
        return True
    scheme = "https" if _https(request) else request.url.scheme
    return origin == f"{scheme}://{request.headers.get('host', '')}"


async def changing_ctx(request: Request) -> Ctx:
    """A state-changing console request: session + same origin + CSRF."""
    ctx = console_ctx(request)
    form = await request.form()
    if not _same_origin(request) or not auth.csrf_ok(ctx.session, form.get("csrf")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "درخواست نامعتبر؛ صفحه را دوباره باز کنید.")
    return ctx


def go(path: str):
    return RedirectResponse(path, status_code=303)


def install_or_404(db, license_id: str, ctx: Ctx):
    install = store.get_install(db, license_id or "")
    if install is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "این لایسنس در فهرست نیست.")
    return install


def row_for(db, install) -> dict:
    now = dt.datetime.utcnow()
    for row in ledger.overview(db, now):
        if row["install"].license_id == install.license_id:
            return row
    raise HTTPException(status.HTTP_404_NOT_FOUND)


