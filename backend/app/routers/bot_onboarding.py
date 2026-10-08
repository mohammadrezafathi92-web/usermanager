"""Small authenticated API surface for Telegram customer onboarding.

Remote bot instances keep no customer database locally; these endpoints let
the in-process and remote implementations share the panel's live settings
and terms-acceptance record.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import models
from ..database import get_db
from ..deps import get_bot_principal
from ..services.bot_auth import BotPrincipal, KeyType, bot_route_policy

router = APIRouter(prefix="/api/bot", tags=["bot-onboarding"], dependencies=[Depends(get_bot_principal)])


def _require_interactive_bot(principal: BotPrincipal) -> None:
    # Customer terms are accepted through the interactive Telegram bot, not
    # by arbitrary sales integrations which can submit any Telegram id.
    if not principal.is_internal and principal.key_type != KeyType.REMOTE_SHARED_BOT:
        raise HTTPException(403, "این عملیات فقط برای ربات تعاملی پنل مجاز است")


class TermsAcceptanceIn(BaseModel):
    telegram_id: int = Field(gt=0)
    terms_digest: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")


@router.get("/customer-onboarding-config")
@bot_route_policy(capability=None, resource_strategy="none")
def get_customer_onboarding_config(
    db: Session = Depends(get_db), principal: BotPrincipal = Depends(get_bot_principal),
):
    _require_interactive_bot(principal)
    owner = db.get(models.AdminUser, principal.owner_admin_id) if principal.owner_admin_id else None
    if principal.owner_admin_id and owner is None:
        raise HTTPException(403, "مالک ربات پیدا نشد")
    row = db.get(models.BotSettings, 1)
    result = {}
    for field in ("required_channel_id", "required_channel_url", "customer_terms_text"):
        # Dedicated bots are independent tenants.  A missing per-owner value
        # means "disabled", not "silently inherit the shared bot".  The old
        # inheritance made every newly-created reseller bot try to inspect the
        # shared channel with its own token; Telegram correctly answered
        # "member list is inaccessible" even when that bot was an admin of its
        # *own* channel.  Shared settings remain exclusive to the shared bot.
        value = getattr(owner, "own_" + field) if owner else getattr(row, field, "")
        result[field] = (value or "").strip()
    return result


@router.get("/customer-terms-acceptance")
@bot_route_policy(capability=None, resource_strategy="none")
def has_customer_accepted_terms(
    telegram_id: int,
    terms_digest: str,
    db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    _require_interactive_bot(principal)
    if telegram_id <= 0 or len(terms_digest) != 64:
        return {"accepted": False}
    exists = db.execute(select(models.BotTermsAcceptance.id).where(
        models.BotTermsAcceptance.telegram_id == telegram_id,
        models.BotTermsAcceptance.terms_digest == terms_digest,
    )).scalar_one_or_none()
    return {"accepted": exists is not None}


@router.post("/customer-terms-acceptance")
@bot_route_policy(capability=None, resource_strategy="none")
def accept_customer_terms(
    payload: TermsAcceptanceIn,
    db: Session = Depends(get_db),
    principal: BotPrincipal = Depends(get_bot_principal),
):
    _require_interactive_bot(principal)
    db.add(models.BotTermsAcceptance(
        telegram_id=payload.telegram_id,
        terms_digest=payload.terms_digest,
    ))
    try:
        db.commit()
    except IntegrityError:
        # Simultaneous taps on the same acceptance are an idempotent success.
        db.rollback()
        if db.execute(select(models.BotTermsAcceptance.id).where(
            models.BotTermsAcceptance.telegram_id == payload.telegram_id,
            models.BotTermsAcceptance.terms_digest == payload.terms_digest,
        )).scalar_one_or_none() is None:
            raise
    return {"accepted": True}
