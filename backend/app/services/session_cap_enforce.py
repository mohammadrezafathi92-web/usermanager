"""The gate in front of session_cap_mode 'enforced'.

preview() is read-only and shows what enforcing the per-Purchase rule would
do to customers right now, and what is still unresolved:

  would_kick_now        WireGuard/Xray connections the first enforced poll
                        would kick that legacy does not (live state now)
  refused_next_login    capped Purchases already at or over their cap: the
                        next PPP login on any of their connections refused
  unrepaired            Purchases with live connections and no cap of their
                        own whose customer has a User cap - today that cap
                        governs them; enforced would fall back to each
                        connection's own cap (repair first:
                        purchase_cap_repair)
  unfrozen_reservations queued renewals with a package from before the
                        renewal cap was frozen: activating them will NOT
                        change the cap
  shadow                the disagreements recorded since shadow began

It returns a token signed over the structural state (the unrepaired and
unfrozen lists, every Purchase's cap, which connection belongs to which
Purchase and customer, the mode epoch) and its generation
time, and a confirmation phrase naming the counts. verify(), called by
session_cap_mode.change_mode under the exclusive lock, refuses unless the
preview is younger than PREVIEW_SECONDS, the state still signs to the same
token, and the phrase was typed back exactly. Live session counts inform the
preview; they do not bind it (they change every poll).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time

from fastapi import HTTPException
from sqlalchemy import func

from .. import models
from ..config import settings
from . import session_cap_rule

PREVIEW_SECONDS = 600


def _live_purchases(db) -> set[int]:
    return {pid for (pid,) in db.query(models.Connection.purchase_id).filter(
        models.Connection.enabled.is_(True), models.Connection.purchase_id.isnot(None)).distinct()}


def _structure(db, row) -> dict:
    live = _live_purchases(db)
    purchases = db.query(models.Purchase).filter(models.Purchase.id.in_(live)).order_by(models.Purchase.id).all() \
        if live else []
    user_caps = dict(db.query(models.User.id, models.User.max_concurrent_sessions).filter(
        models.User.id.in_({p.user_id for p in purchases})).all()) if purchases else {}
    packages = dict(db.query(models.Package.id, models.Package.max_concurrent_sessions).all())
    unrepaired = [[p.id, p.user_id, user_caps.get(p.user_id)] for p in purchases
                  if not p.max_concurrent_sessions and (user_caps.get(p.user_id) or 0) > 0
                  and p.status == models.UserStatus.active]
    unfrozen = [[p.id, p.reserved_package_id, packages.get(p.reserved_package_id), p.max_concurrent_sessions]
                for p in purchases if p.reserved_package_id is not None and p.reserved_max_concurrent_sessions is None]
    caps = [[p.id, p.max_concurrent_sessions] for p in purchases]
    # Which connection belongs to which Purchase and customer: a connection
    # added, removed or moved after the preview changes the scopes it showed.
    members = [list(m) for m in db.query(models.Connection.id, models.Connection.purchase_id,
                                         models.Connection.user_id).order_by(models.Connection.id).all()]
    return dict(epoch=row.epoch if row is not None else 0, unrepaired=unrepaired, unfrozen_reservations=unfrozen,
                caps_digest=hashlib.sha256(json.dumps(caps).encode()).hexdigest(),
                members_digest=hashlib.sha256(json.dumps(members).encode()).hexdigest(),
                capped=sum(1 for c in caps if c[1]))


def _sign(structure: dict, generated_at: int) -> str:
    body = json.dumps(dict(structure, generated_at=generated_at), sort_keys=True).encode()
    return hmac.new(settings.secret_key.encode(), b"session-cap-enforce|" + body, hashlib.sha256).hexdigest()


def _phrase(structure: dict) -> str:
    return (f"ENFORCE {structure['capped']} CAPPED {len(structure['unrepaired'])} UNREPAIRED "
            f"{len(structure['unfrozen_reservations'])} UNFROZEN")


def preview(db) -> dict:
    row = db.get(models.SessionCapRuntimeState, 1)
    structure = _structure(db, row)
    users = (db.query(models.User).filter(models.User.id.in_(
        {p for (p,) in db.query(models.Purchase.user_id).filter(models.Purchase.max_concurrent_sessions > 0)}
        | {u for (u,) in db.query(models.User.id).filter(models.User.max_concurrent_sessions > 0)}
        | {u for (u,) in db.query(models.Connection.user_id).filter(models.Connection.session_limited.is_(True))}
    )).all())
    counts = session_cap_rule.ppp_counts(db, [u.id for u in users])
    would_kick, refused = [], []
    for user in users:
        new_kicks, _ = session_cap_rule.plan(user, session_cap_rule.capped_scopes(user), counts)
        old_kicks, _ = session_cap_rule.plan(user, session_cap_rule.legacy_scopes(user), counts)
        old_ids = {c.id for c, _l, _n in old_kicks}
        would_kick += [dict(connection_id=c.id, purchase_id=c.purchase_id, user_id=user.id, cap=limit, live=count)
                       for c, limit, count in new_kicks if c.id not in old_ids]
        for limit, scope in session_cap_rule.capped_scopes(user):
            if limit and scope[0].purchase_id is not None:
                live = sum(counts.get(c.id, 0) for c in scope if c.type in session_cap_rule.PPP_TYPES) + sum(
                    1 for c in scope if c.type not in session_cap_rule.PPP_TYPES and c.online)
                if live >= limit:
                    refused.append(dict(purchase_id=scope[0].purchase_id, user_id=user.id, cap=limit, live=live))
    shadow = {}
    if row is not None and row.mode == "shadow" and row.changed_at is not None:
        E = models.SessionCapShadowEvent
        shadow = dict(since=row.changed_at, by_kind={k: dict(buckets=b, purchases=p) for k, b, p in db.query(
            E.kind, func.count(), func.count(func.distinct(E.purchase_id))).filter(
            E.first_at >= row.changed_at).group_by(E.kind).all()})
    generated_at = int(time.time())
    db.rollback()
    return dict(mode=row.mode if row is not None else "legacy", **{k: v for k, v in structure.items()},
                would_kick_now=would_kick, refused_next_login=refused, shadow=shadow,
                generated_at=generated_at, token=_sign(structure, generated_at),
                confirmation_phrase=_phrase(structure), expires_in_seconds=PREVIEW_SECONDS)


def verify(db, row, *, token: str, generated_at: int, confirmation: str) -> None:
    """Under session_cap_mode.change_mode's exclusive lock and transaction."""
    if not isinstance(generated_at, int) or not 0 <= time.time() - generated_at <= PREVIEW_SECONDS:
        raise HTTPException(409, "enforce_preview_expired")
    structure = _structure(db, row)
    if not hmac.compare_digest(_sign(structure, generated_at), str(token or "")):
        raise HTTPException(409, "enforce_preview_stale")
    if (confirmation or "").strip() != _phrase(structure):
        raise HTTPException(409, "enforce_confirmation_mismatch")
