"""Routes for issuing and renewing a licence (pages in issue_views.py)."""
from __future__ import annotations

import datetime as dt
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, status

import issue_views
import ledger
import signing
import store
from console_deps import Ctx, changing_ctx, console_ctx, get_db, install_or_404, row_for

router = APIRouter()

# --------------------------------------------------------------------------
# issue and renew: form -> review -> sign (once per review)
# --------------------------------------------------------------------------
def _previous(db, renews: str, ctx: Ctx):
    if not renews:
        return None
    return row_for(db, install_or_404(db, renews, ctx))


def _fingerprint_source(previous) -> str:
    if previous is None:
        return ""
    if previous["issued"] is not None and previous["issued"].fingerprint:
        return "از لایسنس قبلی (امضاشده) پر شده است."
    if previous["install"].fingerprint:
        return "از آخرین heartbeat پنل پر شده است؛ پیش از صدور با پنل مشتری مقایسه کنید."
    return ""


def _defaults(previous) -> dict:
    if previous is None:
        return {"scope": store.DEFAULT_LOCK_SCOPE}
    install, issued = previous["install"], previous["issued"]
    fp = (issued.fingerprint if issued is not None and issued.fingerprint else install.fingerprint) or ""
    return {"customer": install.label or (issued.customer if issued else ""), "fingerprint": fp,
            "scope": install.lock_scope, "renews": install.license_id}


@router.get("/console/issue")
def issue_form(renews: str = "", db=Depends(get_db), ctx: Ctx = Depends(console_ctx)):
    previous = _previous(db, renews, ctx)
    return issue_views.form_page(_defaults(previous), ctx.csrf, previous=previous,
                                 key_ready=signing.has_signing_key(), fingerprint_source=_fingerprint_source(previous))


async def _form_values(request: Request) -> dict:
    form = await request.form()
    return {k: form.get(k) or "" for k in ("customer", "fingerprint", "any_machine", "basis", "amount",
                                         "until", "scope", "note", "renews", "nonce")}


def _plan(db, values: dict, ctx: Ctx):
    previous = _previous(db, values.get("renews", ""), ctx)
    expiry = previous["validity"].get("expires_at") if previous else None
    p, errors = issue_views.plan(values, expiry, dt.datetime.utcnow())
    return previous, p, errors


@router.post("/console/issue/review")
async def issue_review(request: Request, db=Depends(get_db), ctx: Ctx = Depends(changing_ctx)):
    values = await _form_values(request)
    previous, p, errors = _plan(db, values, ctx)
    if errors or not signing.has_signing_key():
        return issue_views.form_page(values, ctx.csrf, previous=previous, errors=errors,
                                     key_ready=signing.has_signing_key(), fingerprint_source=_fingerprint_source(previous))
    nonce = secrets.token_urlsafe(16)
    ctx.session.issues[nonce] = None
    return issue_views.review_page(p, ctx.csrf, nonce, previous)


@router.post("/console/issue/edit")
async def issue_edit(request: Request, db=Depends(get_db), ctx: Ctx = Depends(changing_ctx)):
    values = await _form_values(request)
    previous = _previous(db, values.get("renews", ""), ctx)
    return issue_views.form_page(values, ctx.csrf, previous=previous, key_ready=signing.has_signing_key(),
                                 fingerprint_source=_fingerprint_source(previous))


@router.post("/console/issue")
async def issue_submit(request: Request, db=Depends(get_db), ctx: Ctx = Depends(changing_ctx)):
    values = await _form_values(request)
    nonce = values.get("nonce", "")
    if nonce not in ctx.session.issues or ctx.session.issues[nonce] is not None:
        return issue_views.already_issued_page(ctx.session.issues.get(nonce), ctx.csrf)
    previous, p, errors = _plan(db, values, ctx)
    if errors:
        return issue_views.form_page(values, ctx.csrf, previous=previous, errors=errors,
                                     fingerprint_source=_fingerprint_source(previous))
    ctx.session.issues[nonce] = ""   # claimed before signing: a parallel resend stops here
    try:
        token, payload = signing.issue_payload(
            customer=p["customer"], fingerprint=p["fingerprint"], days=p["days"], valid_days=p["valid_days"],
            until=p["expires_at"] if p["basis"] in ("until", "after") else None, note=p["note"])
    except signing.SigningKeyMissing:
        ctx.session.issues[nonce] = None
        raise HTTPException(status.HTTP_409_CONFLICT, "کلید خصوصی امضا روی این سرور پیدا نشد.")
    renews = previous["install"].license_id if previous else None
    store.register_issued(db, license_id=payload["id"], fingerprint=p["fingerprint"] or None, label=p["customer"])
    install = store.get_install(db, payload["id"])
    install.lock_scope = p["scope"]
    ledger.record_issued(db, payload, source="console", renews=renews, commit=False)
    ledger.log_event(db, "renewed" if renews else "issued", license_id=payload["id"],
                     detail={"basis": p["basis"], "expires_at": payload["exp"], "valid_days": payload["vd"],
                             "bound": bool(p["fingerprint"]), "lock_scope": p["scope"], "renews": renews},
                     ip=ctx.ip, session=ctx.tag, commit=False)
    if renews:
        ledger.log_event(db, "renewal_issued", license_id=renews, detail={"successor": payload["id"]},
                         ip=ctx.ip, session=ctx.tag, commit=False)
    db.commit()
    ctx.session.issues[nonce] = payload["id"]
    return issue_views.result_page(token, payload, renews or "", ctx.csrf)
