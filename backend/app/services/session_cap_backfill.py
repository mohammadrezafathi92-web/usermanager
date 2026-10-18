"""Startup backfill of the per-Purchase concurrent-session caps, so a panel
updates into session_cap_mode 'enforced' (the default) with nothing to run
by hand. Idempotent, at every startup; it only fills what is missing and
never lowers a value:

  Purchase.max_concurrent_sessions       empty -> the cap of the package
                                         linked to THAT Purchase
  Purchase.reserved_max_concurrent_sessions
                                         empty, with a queued renewal ->
                                         the cap of the renewal's package
  a RADIUS PPP connection's own cap (OpenVPN, L2TP, IKEv2, SSTP) of a capped
  Purchase                               empty or lower -> the Purchase's
                                         cap, so the panel shows the cap in
                                         force and a switch back to legacy
                                         keeps it per credential

A Purchase without a linked package (or whose package has no cap) is left
as it is. Each Purchase field filled is audited in purchase_cap_repairs
(admin_id 0: the system). A failure is logged and never stops startup.
"""
from __future__ import annotations

import json
import logging

from .. import models
from . import session_cap_rule

logger = logging.getLogger("session_cap_backfill")

PPP = session_cap_rule.PPP_TYPES  # the RADIUS-authenticated ones; their own cap is per credential
REASON = "startup backfill from the linked package"


def run(session_factory) -> dict:
    try:
        with session_factory() as db:
            counts = _fill(db)
            db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("session cap backfill failed; caps left as they were")
        return {}
    if any(counts.values()):
        logger.info("session cap backfill: %s", counts)
    return counts


def _fill(db) -> dict:
    caps = {pid: cap for pid, cap in db.query(models.Package.id, models.Package.max_concurrent_sessions)
            if cap and cap > 0}
    counts = dict(purchase_caps=0, reserved_caps=0, connection_caps=0)

    def audit(purchase, field, after):
        scope = [cid for (cid,) in db.query(models.Connection.id).filter(
            models.Connection.purchase_id == purchase.id).order_by(models.Connection.id)]
        db.add(models.PurchaseCapRepair(
            purchase_id=purchase.id, user_id=purchase.user_id, field=field, before_value=None, after_value=after,
            user_cap_at_repair=None, scope_connection_ids=json.dumps(scope), preview_token="startup-backfill",
            reason=REASON, admin_id=0))

    for purchase in db.query(models.Purchase).filter(
            models.Purchase.max_concurrent_sessions.is_(None), models.Purchase.package_id.isnot(None)):
        cap = caps.get(purchase.package_id)
        if cap:
            purchase.max_concurrent_sessions = cap
            audit(purchase, "max_concurrent_sessions", cap)
            counts["purchase_caps"] += 1
    for purchase in db.query(models.Purchase).filter(
            models.Purchase.reserved_max_concurrent_sessions.is_(None), models.Purchase.reserved_package_id.isnot(None)):
        cap = caps.get(purchase.reserved_package_id)
        if cap:
            purchase.reserved_max_concurrent_sessions = cap
            audit(purchase, "reserved_max_concurrent_sessions", cap)
            counts["reserved_caps"] += 1
    db.flush()
    rows = (db.query(models.Connection, models.Purchase.max_concurrent_sessions)
            .join(models.Purchase, models.Purchase.id == models.Connection.purchase_id)
            .filter(models.Purchase.max_concurrent_sessions > 0, models.Connection.type.in_(PPP)))
    for connection, cap in rows:
        if (connection.max_concurrent_sessions or 0) < cap:
            connection.max_concurrent_sessions = cap
            counts["connection_caps"] += 1
    return counts
