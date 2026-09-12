"""Turns a usage-billed reseller's consumed traffic into money owed.

A "usage" billing_mode admin (see models.AdminUser.billing_mode) is never
charged at package-creation time. Their GB pool depletes in near-real-time
in quota_manager._apply_delta instead - which, until 2026-09, was the ONLY
thing that happened: nothing priced that traffic, so nothing about it ever
reached the books. Their cost of goods read as zero, every margin they
were shown was 100%, and the panel owner had no figure at all for what a
usage-billed reseller owed them.

This module closes that. _apply_delta now also adds the same GB onto
AdminUser.unbilled_usage_gb, and settle_usage_charges() drains that
counter once a day into ONE admin_usage_charge ledger row per admin,
priced at that admin's own wholesale_price_per_gb.

Daily and rolled up, not per poll, on purpose: at a 30s poll interval a
per-event row would add thousands of ledger rows per active connection per
day - the same mistake models.UsageLog had to be rescued from. A day's
traffic as a single priced line is also what a human actually wants to
read in the transactions list.

An admin with no rate set (wholesale_price_per_gb = 0) is metered but not
priced: their GB still deplete, the counter still drains, and no money row
is written - same deliberate "don't invent a price the operator never set"
rule as admin_billing.charge_for_renewal's raw-gigabyte path.
"""
from __future__ import annotations

import logging

from sqlalchemy import update

from .. import models
from ..database import SessionLocal
from . import accounting

logger = logging.getLogger("usage_billing")

# Below this, leave it on the counter and let it accumulate into tomorrow's
# row instead of writing a row worth a few tomans. Keeps the ledger
# readable for an admin whose users are idle.
MIN_BILLABLE_GB = 0.01


def settle_usage_charges(db=None) -> int:
    """Drains every usage-billed admin's unbilled_usage_gb into a priced
    admin_usage_charge row. Returns how many admins were charged.

    The counter is zeroed with the SAME atomic UPDATE that reads it (a
    conditional decrement of exactly the amount billed, not a blind reset
    to 0), so traffic metered by the poller in the middle of this function
    is carried into the next run instead of being silently thrown away.
    """
    own_session = db is None
    db = db or SessionLocal()
    try:
        admins = (
            db.query(models.AdminUser)
            .filter(
                models.AdminUser.is_superadmin.is_(False),
                models.AdminUser.billing_mode == "usage",
                models.AdminUser.unbilled_usage_gb >= MIN_BILLABLE_GB,
            )
            .all()
        )
        charged = 0
        for admin in admins:
            gb = float(admin.unbilled_usage_gb or 0)
            if gb < MIN_BILLABLE_GB:
                continue
            rate = int(admin.wholesale_price_per_gb or 0)

            # Subtract exactly what we are about to bill for - anything the
            # poller added since the read above stays on the counter.
            db.execute(
                update(models.AdminUser)
                .where(models.AdminUser.id == admin.id)
                .values(unbilled_usage_gb=models.AdminUser.unbilled_usage_gb - gb)
            )

            if rate > 0:
                amount = int(round(gb * rate))
                if amount > 0:
                    accounting.record(
                        db, "admin_usage_charge", amount,
                        admin_id=admin.id,
                        note=f"مصرف {gb:.2f} گیگابایت × {rate:,} تومان",
                    )
                    charged += 1
            db.commit()
        if charged:
            logger.info("usage billing: charged %d usage-billed admin(s)", charged)
        return charged
    except Exception:
        logger.exception("usage billing failed")
        db.rollback()
        return 0
    finally:
        if own_session:
            db.close()
