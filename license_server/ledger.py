"""What the console knows about a licence beyond its heartbeat - and the
record of what the operator did.

Three facts about an install are kept apart on purpose, because mixing
them is how a console misleads its operator:

  * validity  - when the signed licence runs out. Known ONLY from a payload
                this server signed or verified (issued_licenses). Anything
                else is "unknown", never guessed from first_seen or a ping.
  * access    - the operator's lever (revoked + lock scope). A revoked
                licence can be perfectly valid; an expired one is not
                revoked.
  * contact   - when the panel last checked in. Silence is neither expiry
                nor revocation.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from typing import Optional

import store
from store import ConsoleEvent, IssuedLicense, Install

STALE_AFTER = dt.timedelta(hours=6)      # the panel default beats every 3 hours
SILENT_AFTER = dt.timedelta(days=3)
EXPIRING_WITHIN = dt.timedelta(days=14)

ACCESS_ACTIONS = ("access_changed", "issued", "renewed")


# --------------------------------------------------------------------------
# the audit trail
# --------------------------------------------------------------------------
def session_tag(token: Optional[str]) -> Optional[str]:
    """A stable, non-reversible name for a console session in the log."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def log_event(db, action: str, *, license_id: Optional[str] = None, detail: Optional[dict] = None,
              reason: Optional[str] = None, ip: Optional[str] = None, session: Optional[str] = None,
              commit: bool = True) -> ConsoleEvent:
    event = ConsoleEvent(
        at=dt.datetime.utcnow(), action=action, license_id=license_id,
        detail=json.dumps(detail, ensure_ascii=False, sort_keys=True) if detail else None,
        reason=(reason or "").strip()[:500] or None, ip=ip, session=session,
    )
    db.add(event)
    if commit:
        db.commit()
    return event


def events(db, *, license_id: Optional[str] = None, limit: int = 200) -> list[ConsoleEvent]:
    query = db.query(ConsoleEvent)
    if license_id is not None:
        query = query.filter(ConsoleEvent.license_id == license_id)
    return query.order_by(ConsoleEvent.id.desc()).limit(limit).all()


def event_detail(event: ConsoleEvent) -> dict:
    try:
        return json.loads(event.detail) if event.detail else {}
    except ValueError:
        return {}


# --------------------------------------------------------------------------
# issued licences
# --------------------------------------------------------------------------
def get_issued(db, license_id: str) -> Optional[IssuedLicense]:
    return db.get(IssuedLicense, license_id)


def record_issued(db, payload: dict, *, source: str, renews: Optional[str] = None,
                  commit: bool = True) -> IssuedLicense:
    """Keep a signed payload's facts. An existing row is never rewritten: the
    first record of a licence id is the one its token was made from."""
    existing = get_issued(db, payload["id"])
    if existing is not None:
        return existing
    row = IssuedLicense(
        license_id=payload["id"], customer=payload.get("customer") or "",
        fingerprint=payload.get("fp") or "",
        issued_at=_parse(payload.get("iat")) or dt.datetime.utcnow(),
        expires_at=_parse(payload.get("exp")), valid_days=payload.get("vd"),
        note=payload.get("note") or None, source=source, renews_license_id=renews,
        recorded_at=dt.datetime.utcnow(),
    )
    db.add(row)
    if commit:
        db.commit()
    return row


def successors(db, license_id: str) -> list[IssuedLicense]:
    return (db.query(IssuedLicense).filter(IssuedLicense.renews_license_id == license_id)
            .order_by(IssuedLicense.issued_at).all())


def lineage(db, license_id: str) -> list[str]:
    """Oldest first: every licence this one continues, then itself, then
    whatever renewed it (following the latest renewal each time)."""
    chain = [license_id]
    seen = {license_id}
    current = get_issued(db, license_id)
    while current is not None and current.renews_license_id and current.renews_license_id not in seen:
        chain.insert(0, current.renews_license_id)
        seen.add(current.renews_license_id)
        current = get_issued(db, current.renews_license_id)
    tail = license_id
    while True:
        nxt = successors(db, tail)
        nxt = [n for n in nxt if n.license_id not in seen]
        if not nxt:
            break
        tail = nxt[-1].license_id
        chain.append(tail)
        seen.add(tail)
    return chain


# --------------------------------------------------------------------------
# the three separate facts
# --------------------------------------------------------------------------
def validity(install: Optional[Install], issued: Optional[IssuedLicense], now: dt.datetime) -> dict:
    """Mirrors the panel's licensing.effective_expiry: an absolute exp wins;
    otherwise valid_days counts from the activation the panel reported."""
    if issued is None:
        return {"state": "unknown", "expires_at": None, "days_left": None}
    expires = issued.expires_at
    if expires is None and issued.valid_days:
        activated = install.activated_at if install is not None else None
        if activated is None:
            return {"state": "not_started", "expires_at": None, "days_left": None,
                    "valid_days": issued.valid_days}
        expires = activated + dt.timedelta(days=int(issued.valid_days))
    if expires is None:
        return {"state": "perpetual", "expires_at": None, "days_left": None}
    if expires <= now:
        return {"state": "expired", "expires_at": expires, "days_left": 0}
    state = "expiring" if expires - now <= EXPIRING_WITHIN else "valid"
    return {"state": state, "expires_at": expires, "days_left": (expires - now).days}


def contact(install: Install, now: dt.datetime) -> dict:
    if not install.heartbeat_count:
        return {"state": "never", "last_seen": None}
    silent = now - install.last_seen
    state = "recent" if silent <= STALE_AFTER else ("late" if silent <= SILENT_AFTER else "silent")
    return {"state": state, "last_seen": install.last_seen, "silent": silent}


def usage(install: Install, now: dt.datetime) -> dict:
    """The last aggregate counts the panel reported, and how old they are.

    Age is measured on THIS server's clock (when the sample arrived), not
    the panel's. Never reported -> unknown, not zero; older than STALE_AFTER
    -> stale, shown with its age instead of as current."""
    if install.stats_received_at is None or install.stats_customer_accounts is None:
        return {"state": "unknown"}
    age = now - install.stats_received_at
    return {
        "state": "fresh" if age <= STALE_AFTER else "stale",
        "accounts": install.stats_customer_accounts,
        "online": install.stats_online_accounts,
        "sessions": install.stats_online_sessions,
        "received_at": install.stats_received_at,
        "sampled_at": install.stats_sampled_at,
        "age": age,
    }


def delivery(db, install: Install) -> dict:
    """Whether the panel has heard the CURRENT access decision.

    A revocation takes effect only on the panel's next heartbeat. The last
    access change recorded here is compared with the last heartbeat; with
    no recorded change (a decision older than this log) it is unknown."""
    last = (db.query(ConsoleEvent).filter(ConsoleEvent.license_id == install.license_id,
                                          ConsoleEvent.action.in_(ACCESS_ACTIONS))
            .order_by(ConsoleEvent.id.desc()).first())
    if last is None:
        return {"state": "unknown", "changed_at": None}
    if install.heartbeat_count and install.last_seen >= last.at:
        return {"state": "delivered", "changed_at": last.at}
    return {"state": "pending", "changed_at": last.at}


def overview(db, now: Optional[dt.datetime] = None) -> list[dict]:
    """Every install with its three facts and its place in a renewal chain."""
    now = now or dt.datetime.utcnow()
    installs = store.list_installs(db)
    issued = {row.license_id: row for row in db.query(IssuedLicense).all()}
    by_id = {i.license_id: i for i in installs}
    renewed_by: dict[str, list[IssuedLicense]] = {}
    for row in issued.values():
        if row.renews_license_id:
            renewed_by.setdefault(row.renews_license_id, []).append(row)
    rows = []
    for install in installs:
        record = issued.get(install.license_id)
        later = sorted(renewed_by.get(install.license_id, []), key=lambda r: r.issued_at)
        successor = later[-1] if later else None
        succ_install = by_id.get(successor.license_id) if successor else None
        rows.append({
            "install": install, "issued": record,
            "validity": validity(install, record, now),
            "contact": contact(install, now),
            "usage": usage(install, now),
            "successor": successor,
            # replaced only once the new key has actually reported in
            "replaced": bool(succ_install is not None and succ_install.heartbeat_count),
            "moved": install.fingerprint_changed_at is not None,
        })
    return rows


def matches(row: dict, *, q: str = "", access: str = "", contact_state: str = "", valid: str = "",
            moved: bool = False) -> bool:
    install = row["install"]
    if moved and not row["moved"]:
        return False
    if q:
        needle = q.strip().lower()
        hay = " ".join(filter(None, [install.label, install.license_id, install.fingerprint,
                                     install.last_ip, install.note,
                                     row["issued"].customer if row["issued"] else None])).lower()
        if needle not in hay:
            return False
    if access == "locked" and not install.revoked:
        return False
    if access == "open" and install.revoked:
        return False
    if contact_state and row["contact"]["state"] != contact_state:
        return False
    if valid:
        state = row["validity"]["state"]
        if valid == "attention" and state not in ("expired", "expiring"):
            return False
        if valid != "attention" and state != valid:
            return False
    return True


def counts(rows: list[dict]) -> dict:
    return {
        "total": len(rows),
        "locked": sum(1 for r in rows if r["install"].revoked),
        "silent": sum(1 for r in rows if r["contact"]["state"] in ("silent", "never")),
        "attention": sum(1 for r in rows if r["validity"]["state"] in ("expired", "expiring")),
        "unknown": sum(1 for r in rows if r["validity"]["state"] == "unknown"),
        "moved": sum(1 for r in rows if r["moved"]),
    }


def _parse(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
