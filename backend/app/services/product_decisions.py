"""Versioned confirmation of the Product decisions the joint activation
(P9a) depends on - Receipt Void 23, items 15, 16, 18 and 19, which "must be
closed before P9a".

The text below is what a superadmin confirms, exactly: its SHA-256 is the
confirmation's content_hash, so a changed text is a new version that needs
its own confirmation. Nothing here is confirmed by default; the activation
refuses with 409 product_decisions_unconfirmed until a superadmin confirmed
THIS version with their password (routers/receipt_approvals.py).
"""
from __future__ import annotations

import datetime as dt
import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import models_reservation_transition as rt

DECISION_SET = "p9a_loyalty"
VERSION = "2026-10-19.1"
TEXT = """\
P9a loyalty decisions (Receipt Void 23, items 15, 16, 18, 19), version 2026-10-19.1

15. When a purchase counts: at payment, the same for a customer's account
    and for each service. A queued (reserved) renewal counts when it is
    paid, never when it starts. Changes from today: a service renewal that
    does not count today counts from now on; a queued account renewal
    counts at payment instead of at its start. A sale of amount 0 does not
    count.

16. What does not count: a panel sale paid from a reseller's credit, and an
    external payment with neither an approved receipt nor a wallet capture.
    Only a sale with backend payment evidence (approved receipt or wallet
    capture) counts. Today every panel sale and every bot sale counts.

18. Changing the threshold after P9a: a new period starts from that moment.
    Rewards already given stay; nothing owed is paid out retroactively;
    progress restarts from the customer's count at the change (example:
    threshold 3 -> 10 with the customer at 6: the next reward is at 16). A
    reward not reached in a closed period cannot be earned later.

19. Starting point at activation: P - (P mod T). Progress inside the current
    cycle is kept; an overdue reward under the old catch-up rule is not
    created (example T=5, P=7: next reward at 10).
"""
CONTENT_HASH = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()


def describe(db) -> dict:
    return {"decision_set": DECISION_SET, "version": VERSION, "content_hash": CONTENT_HASH, "text": TEXT,
            "confirmed": confirmation(db)}


def confirmation(db):
    table = rt.product_decision_confirmations
    row = db.execute(select(table).where(table.c.decision_set == DECISION_SET, table.c.version == VERSION,
                                         table.c.content_hash == CONTENT_HASH)).mappings().first()
    return None if row is None else {"id": int(row["id"]), "confirmed_by_admin_id": int(row["confirmed_by_admin_id"]),
                                     "created_at": row["created_at"]}


def confirm(db, admin_id: int, *, version: str, content_hash: str) -> dict:
    """Commits. The caller must name the exact version and hash it read."""
    if version != VERSION or content_hash != CONTENT_HASH:
        raise HTTPException(409, "product_decisions_version_mismatch")
    if confirmation(db) is None:
        try:
            db.execute(rt.product_decision_confirmations.insert().values(
                decision_set=DECISION_SET, version=VERSION, content_hash=CONTENT_HASH,
                confirmed_by_admin_id=admin_id, created_at=dt.datetime.utcnow()))
            db.commit()
        except IntegrityError:
            db.rollback()                       # a concurrent confirmation of the same version
    return describe(db)


def require_confirmed(db) -> None:
    if confirmation(db) is None:
        raise HTTPException(409, {"error": "product_decisions_unconfirmed", "items": [
            {"decision_set": DECISION_SET, "version": VERSION, "content_hash": CONTENT_HASH}]})
