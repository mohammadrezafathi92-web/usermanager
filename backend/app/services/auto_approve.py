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
from types import SimpleNamespace

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
    if getattr(settings, "auto_approve_ignore_hours", False):
        return True
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


def _effective_settings(db, pending: dict) -> tuple[object | None, bool]:
    """(settings, is_own_override). `settings` exposes the same
    auto_approve_enabled/ignore_hours/from_hour/to_hour/max_amount/
    returning_only attributes as models.BotSettings, regardless of which of
    the two sources below it actually came from - so _in_window and the
    rest of decide() below don't need to know or care which one it is.

    Precedence: the owning Admin's/Seller's OWN override
    (AdminUser.own_auto_approve_*) if they've explicitly configured one -
    own_auto_approve_enabled is not NULL - else the single global
    BotSettings row, unchanged from this feature's original (pre-per-
    reseller) behavior. See models.AdminUser's docstring on those columns
    for why NULL, not per-field fallback, is the discriminator.
    """
    owner_admin_id = pending.get("owner_admin_id")
    if owner_admin_id:
        owner = db.get(models.AdminUser, owner_admin_id)
        if owner is not None and owner.own_auto_approve_enabled is not None:
            return (
                SimpleNamespace(
                    auto_approve_enabled=owner.own_auto_approve_enabled,
                    auto_approve_ignore_hours=bool(owner.own_auto_approve_ignore_hours),
                    auto_approve_from_hour=(
                        owner.own_auto_approve_from_hour if owner.own_auto_approve_from_hour is not None else 9
                    ),
                    auto_approve_to_hour=(
                        owner.own_auto_approve_to_hour if owner.own_auto_approve_to_hour is not None else 23
                    ),
                    auto_approve_max_amount=owner.own_auto_approve_max_amount or 0,
                    auto_approve_returning_only=(
                        owner.own_auto_approve_returning_only
                        if owner.own_auto_approve_returning_only is not None
                        else True
                    ),
                ),
                True,
            )
    return db.get(models.BotSettings, 1), False


def decide(pending: dict) -> tuple[bool, str]:
    """(allowed, reason). The reason is logged either way - "why was this
    NOT auto-approved" is the question that gets asked in practice."""
    if pending.get("kind") not in AUTO_APPROVABLE_KINDS:
        return False, f"نوع درخواست ({pending.get('kind')}) مشمول تایید خودکار نیست"

    db = SessionLocal()
    try:
        settings, own_override = _effective_settings(db, pending)
        scope_note = " (تنظیمات اختصاصی فروشنده)" if own_override else ""
        if settings is None or not settings.auto_approve_enabled:
            return False, f"تایید خودکار خاموش است{scope_note}"

        now = dt.datetime.utcnow()
        if not _in_window(settings, now):
            return False, f"خارج از ساعات تایید خودکار{scope_note}"

        cap = int(settings.auto_approve_max_amount or 0)
        amount = _amount_of(pending)
        if cap and amount > cap:
            return False, f"مبلغ {amount:,} از سقف {cap:,} بیشتر است{scope_note}"

        if settings.auto_approve_returning_only and not _is_returning(db, pending):
            return False, f"مشتری خرید تاییدشده‌ی قبلی ندارد{scope_note}"

        return True, f"مبلغ {amount:,} - همه شرط‌ها برقرار است{scope_note}"
    except Exception:
        # A failure to decide is a decision not to approve.
        logger.exception("auto-approve check failed - falling back to manual approval")
        return False, "بررسی تایید خودکار با خطا مواجه شد"
    finally:
        db.close()


async def try_auto_approve(pending: dict, bot) -> tuple[bool, str]:
    """(approved, reason). Claims the request the same way the Approve
    button does, so an admin tapping Approve at the same moment cannot
    double-provision: whichever gets the claim first wins and the other
    sees "already handled".

    The reason is RETURNED, not just logged. It used to be logged only,
    which is why this feature could be "not working" with no way to tell
    which condition was refusing - the answer existed, inside a container,
    where nobody was going to look for it.
    """
    allowed, reason = decide(pending)
    if not allowed:
        logger.info("درخواست %s تایید خودکار نشد: %s", pending.get("id"), reason)
        return False, reason

    from ..telegram_bot import storage as bot_storage
    from ..telegram_bot.handlers.admin_pending import perform_approval

    if not bot_storage.claim_pending(pending["id"]):
        logger.info("درخواست %s همین الان توسط ادمین رسیدگی شد", pending.get("id"))
        return False, "همین الان توسط ادمین رسیدگی شد"

    ok, result = await perform_approval(pending, bot)
    if not ok:
        # perform_approval already released the claim on failure, so the
        # request is back in the admins' queue rather than lost.
        logger.warning("تایید خودکار درخواست %s ناموفق بود: %s", pending.get("id"), result)
        return False, f"تایید خودکار شروع شد ولی ناموفق بود: {result}"

    logger.info("درخواست %s به‌صورت خودکار تایید شد (%s)", pending.get("id"), reason)
    await _notify_admins(pending, bot, reason)
    return True, reason


async def _notify_admins(pending: dict, bot, reason: str) -> None:
    """Tells the admins a sale happened without them.

    Without this the feature is silent: the owner would find out about
    automatic approvals only by noticing the money. There is no approve
    button on this message - the service is already delivered - so it is
    plain text, deliberately distinguishable from a request awaiting action.
    """
    from ..telegram_bot.handlers.admin_pending import _pending_summary, _owner_label
    from ..telegram_bot.handlers.customer import _notify_targets

    try:
        owner_label = await _owner_label(pending.get("owner_admin_id"))
        text = "🤖 تایید خودکار انجام شد\n\n" + _pending_summary(pending, owner_label) + f"\n\nدلیل: {reason}"
    except Exception:
        logger.exception("could not render the auto-approval notice")
        text = f"🤖 درخواست {pending.get('id')} به‌صورت خودکار تایید شد"

    # See customer.py's _notify_targets docstring - approval_targets() alone
    # can miss the actual owning reseller, so their own linked Telegram id
    # (from pending['owner_admin_id']) is always added too.
    for admin_id in await _notify_targets(pending):
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            # A notification failure must never undo a completed approval.
            logger.warning("ارسال اطلاع تایید خودکار به %s ناموفق بود", admin_id)
