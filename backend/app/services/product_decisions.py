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
VERSION = "2026-10-20.1"
TEXT = """\
P9a loyalty decisions (Receipt Void 23, items 15, 16, 18, 19), version 2026-10-20.1
Approved by the owner on 2026-10-20 (relayed in writing); recording the
confirmation in a panel is still a superadmin's act through this API.

15. When a purchase counts: a purchase or a renewal with an amount above
    zero, paid with an approved receipt or a capture from the customer's
    wallet, counts exactly once, at payment. The start of a queued (reserved)
    renewal counts nothing and grants nothing again.

16. What does not count: a sale paid from a seller's credit, a free service,
    an amount of zero, and an external payment without backend evidence. A
    wallet top-up is not itself a purchase; the later purchase paid from that
    balance counts.

18. Changing the threshold later: a new period starts from the customer's
    count at that moment (example: threshold changed to 10 when the customer
    is at 6: the next reward is at 16). Rewards already given stay.

19. Starting point at the first activation: current progress is kept
    (threshold 5, count 7: the next reward is at 10); no overdue historical
    reward is created.
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
