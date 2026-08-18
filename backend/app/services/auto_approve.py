"""«تایید خودکار رسید» - decides whether a payment receipt may be approved
without a human looking at it.

This module only ever DECIDES. The approving itself is
telegram_bot/handlers/admin_pending.perform_approval - the same function the
Approve button calls - so there is exactly one implementation of "approve a
payment". Two would drift, and the half that drifts is the half nobody is
watching.

Every check here fails CLOSED: any doubt, any error, any unreadable setting
sends the request back to manual approval. The cost of a wrong "no" is that
an admin taps a button; the cost of a wrong "yes" is a service given away.
"""
from __future__ import annotations

import datetime as dt
import logging

from .. import models
from ..database import SessionLocal
from .jalali import get_display_offset

logger = logging.getLogger("auto_approve")

# Only purchases and renewals. A "link" request (claiming an existing
# account) and a wallet top-up are deliberately excluded: linking is an
# identity claim that no amount check can validate, and a top-up has no
# package to bound the risk.
AUTO_APPROVABLE_KINDS = ("new", "renew")


def _in_window(settings: models.BotSettings, now: dt.datetime) -> bool:
    """Local hours, not UTC - an admin setting "9 to 23" means their own
    clock. Same convention and wrap-past-midnight handling as the ads
    scheduler (services/ads.py)."""
    start = settings.auto_approve_from_hour if settings.auto_approve_from_hour is not None else 0
    end = settings.auto_approve_to_hour if settings.auto_approve_to_hour is not None else 0
    if start == end:
        return True     # equal values mean "any time", not "never"
    hour = (now + dt.timedelta(minutes=get_display_offset())).hour
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _amount_of(pending: dict) -> int:
    """What the customer actually owed, after any discount."""
    value = pending.get("final_price")
    if value is None:
        value = pending.get("price")
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _is_returning(db, pending: dict) -> bool:
    """True only if this telegram account has an APPROVED purchase already.

    Deliberately checks the bot's own request history rather than "does a
    user row exist": a customer row can be created by an admin, by an
    import, or by a link request, none of which is evidence that this person
    has ever actually paid.
    """
    from ..telegram_bot import storage as bot_storage

    telegram_id = pending.get("telegram_id")
    if not telegram_id:
        return False
    try:
        rows = bot_storage.list_recent(limit=500)
    except Exception:
        logger.exception("could not read the request history - treating as a first-time buyer")
        return False
    return any(
        r.get("telegram_id") == telegram_id
        and r.get("status") == "approved"
        and r.get("id") != pending.get("id")
        and r.get("kind") in AUTO_APPROVABLE_KINDS
        for r in rows
    )


def decide(pending: dict) -> tuple[bool, str]:
    """(allowed, reason). The reason is logged either way - "why was this
    NOT auto-approved" is the question that gets asked in practice."""
    if pending.get("kind") not in AUTO_APPROVABLE_KINDS:
        return False, f"نوع درخواست ({pending.get('kind')}) مشمول تایید خودکار نیست"

    db = SessionLocal()
    try:
        settings = db.get(models.BotSettings, 1)
        if settings is None or not settings.auto_approve_enabled:
            return False, "تایید خودکار خاموش است"

        now = dt.datetime.utcnow()
        if not _in_window(settings, now):
            return False, "خارج از ساعات تایید خودکار"

        cap = int(settings.auto_approve_max_amount or 0)
        amount = _amount_of(pending)
        if cap and amount > cap:
            return False, f"مبلغ {amount:,} از سقف {cap:,} بیشتر است"

        if settings.auto_approve_returning_only and not _is_returning(db, pending):
            return False, "مشتری خرید تاییدشده‌ی قبلی ندارد"

        return True, f"مبلغ {amount:,} - همه شرط‌ها برقرار است"
    except Exception:
        # A failure to decide is a decision not to approve.
        logger.exception("auto-approve check failed - falling back to manual approval")
        return False, "بررسی تایید خودکار با خطا مواجه شد"
    finally:
        db.close()


async def try_auto_approve(pending: dict, bot) -> bool:
    """Approves the request if it qualifies. Returns whether it did.

    Claims the request the same way the Approve button does, so an admin
    tapping Approve at the same moment cannot double-provision: whichever
    gets the claim first wins and the other sees "already handled".
    """
    allowed, reason = decide(pending)
    if not allowed:
        logger.info("درخواست %s تایید خودکار نشد: %s", pending.get("id"), reason)
        return False

    from ..telegram_bot import storage as bot_storage
    from ..telegram_bot.handlers.admin_pending import perform_approval

    if not bot_storage.claim_pending(pending["id"]):
        logger.info("درخواست %s همین الان توسط ادمین رسیدگی شد", pending.get("id"))
        return False

    ok, result = await perform_approval(pending, bot)
    if not ok:
        # perform_approval already released the claim on failure, so the
        # request is back in the admins' queue rather than lost.
        logger.warning("تایید خودکار درخواست %s ناموفق بود: %s", pending.get("id"), result)
        return False

    logger.info("درخواست %s به‌صورت خودکار تایید شد (%s)", pending.get("id"), reason)
    await _notify_admins(pending, bot, reason)
    return True


async def _notify_admins(pending: dict, bot, reason: str) -> None:
    """Tells the admins a sale happened without them.

    Without this the feature is silent: the owner would find out about
    automatic approvals only by noticing the money. There is no approve
    button on this message - the service is already delivered - so it is
    plain text, deliberately distinguishable from a request awaiting action.
    """
    from ..telegram_bot.config import config
    from ..telegram_bot.handlers.admin_pending import _pending_summary

    try:
        text = "🤖 تایید خودکار انجام شد\n\n" + _pending_summary(pending) + f"\n\nدلیل: {reason}"
    except Exception:
        logger.exception("could not render the auto-approval notice")
        text = f"🤖 درخواست {pending.get('id')} به‌صورت خودکار تایید شد"

    for admin_id in config.approval_targets():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            # A notification failure must never undo a completed approval.
            logger.warning("ارسال اطلاع تایید خودکار به %s ناموفق بود", admin_id)
