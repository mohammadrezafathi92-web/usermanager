"""Operator endpoints for the receipt-approval runtime mode (Receipt Void
design 5.5, section 18). Superadmin only; a mode change also asks for the
admin's own password."""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..database import SessionLocal, engine, get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import receipt_approval_runtime as runtime
from ..services import receipt_void_schema

router = APIRouter(
    prefix="/api/accounting/receipt-approvals", tags=["receipt-approvals"],
    dependencies=[Depends(require_superadmin)],
)


class RuntimeModeUpdate(BaseModel):
    registration_mode: Optional[str] = None
    auto_rate_limit_mode: Optional[str] = None


@router.get("/runtime-mode")
def get_runtime_mode(db: Session = Depends(get_db)):
    return runtime.describe(db)


@router.put("/runtime-mode")
def put_runtime_mode(payload: RuntimeModeUpdate, db: Session = Depends(get_db),
                     _confirm=Depends(require_confirm_password)):
    if not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    try:
        runtime.set_requested_mode(db, registration_mode=payload.registration_mode,
                                   auto_rate_limit_mode=payload.auto_rate_limit_mode)
        db.commit()
    except runtime.ModeChangeRejected as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=exc.code)
    return runtime.describe(db)


@router.post("/runtime-mode/recheck")
def recheck_runtime_mode(db: Session = Depends(get_db)):
    if not receipt_void_schema.is_ready():
        raise HTTPException(status_code=503, detail="receipt_void_schema_not_ready")
    receipt_void_schema.compute_key_identity(engine, SessionLocal)
    return runtime.describe(db)


# ---------------------------------------------------------------- registration (bots)
# Called by the sales bots (X-API-Key, or in-process), never by a panel
# admin session - hence a router of its own with no superadmin dependency.
from fastapi import Response  # noqa: E402

from ..deps import get_bot_principal  # noqa: E402
from ..services import bot_auth  # noqa: E402
from ..services import receipt_approval_intent as intents  # noqa: E402
from ..services import receipt_approval_registration as registration  # noqa: E402

bot_router = APIRouter(prefix="/api/accounting/receipt-approvals", tags=["receipt-approvals"])


class ConnectionSpecIn(BaseModel):
    node_id: int
    protocol: str
    flow: Optional[str] = ""


class ApprovalIntentIn(BaseModel):
    pending_source_instance_id: str
    pending_local_id: int
    kind: str
    target_username: str
    amount: int
    package_id: Optional[int] = None
    renew_purchase_id: Optional[int] = None
    payment_card_id: Optional[int] = None
    receipt_file_id: Optional[str] = None
    discount_code: Optional[str] = None
    referral_code: Optional[str] = None
    list_price: Optional[int] = None
    connections: list[ConnectionSpecIn] = []
    # identities are only ever COMPARED with what the backend derives
    telegram_id: Optional[int] = None
    owner_admin_id: Optional[int] = None
    approved_by_telegram_id: Optional[int] = None      # manual only
    local_decision: Optional[str] = None               # auto only: the bot's own verdict, logged, never trusted

    def to_intent(self) -> intents.ApprovalIntent:
        return intents.ApprovalIntent(
            pending_source_instance_id=self.pending_source_instance_id, pending_local_id=self.pending_local_id,
            kind=self.kind, target_username=self.target_username, amount=self.amount, package_id=self.package_id,
            renew_purchase_id=self.renew_purchase_id, payment_card_id=self.payment_card_id,
            receipt_file_id=self.receipt_file_id, discount_code=self.discount_code, referral_code=self.referral_code,
            list_price=self.list_price,
            connections=tuple(intents.ConnectionSpec(c.node_id, c.protocol, c.flow or "") for c in self.connections),
            claimed_telegram_id=self.telegram_id, claimed_owner_admin_id=self.owner_admin_id)


def begin_approval(db: Session, principal: bot_auth.BotPrincipal, payload: ApprovalIntentIn, approval_mode: str) -> dict:
    """Shared by both endpoints and by the in-process bot. The mode comes
    from which endpoint was called, never from a body field."""
    bot_auth.require_bot_capability(
        principal,
        bot_auth.RECEIPT_MANUAL_APPROVAL_WRITE if approval_mode == registration.MANUAL else bot_auth.RECEIPT_AUTO_APPROVAL_WRITE,
        endpoint=f"receipt_approval_{approval_mode}")
    try:
        return registration.begin(
            db, principal, payload.to_intent(), approval_mode=approval_mode,
            approved_by_telegram_id=payload.approved_by_telegram_id if approval_mode == registration.MANUAL else None,
            local_decision=payload.local_decision if approval_mode == registration.AUTO else None)
    except registration.RegistrationRejected as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code)


@bot_router.post("/auto")
def register_auto(payload: ApprovalIntentIn, response: Response, db: Session = Depends(get_db),
                  principal: bot_auth.BotPrincipal = Depends(get_bot_principal)):
    body = begin_approval(db, principal, payload, registration.AUTO)
    response.status_code = 201 if body.get("created") else 200
    return body


class FinalizeIn(BaseModel):
    failed: bool = False                # the bot reports that the approval did not go through


def finalize_approval(db: Session, principal: bot_auth.BotPrincipal, approval_uuid: str, failed: bool = False) -> dict:
    """Shared by the endpoint and the in-process bot."""
    bot_auth._ensure_valid(principal, "receipt_approval_finalize")
    if not (bot_auth.MANAGED_BOT_CAPABILITIES & principal.capabilities):        # either of the two (design 6.4)
        bot_auth.require_bot_capability(principal, bot_auth.RECEIPT_AUTO_APPROVAL_WRITE, endpoint="receipt_approval_finalize")
    for attempt in range(1, registration.WRITE_ATTEMPTS + 1):
        try:
            registration.take_write_lock(db)
            mode_state = registration.runtime.read_state(db, lock=True, shared_lock=True)
            if registration.runtime.effective_registration_mode(mode_state) == registration.runtime.BLOCKED:
                raise registration.RegistrationRejected(503, "receipt_approval_blocked")
            result = registration.finalize(db, principal, approval_uuid, reported_failure=failed)
            db.commit()
            return result
        except registration.RegistrationRejected as exc:
            db.rollback()
            raise HTTPException(status_code=exc.status, detail=exc.code)
        except OperationalError:                 # database busy: wait and try again, as begin() does
            db.rollback()
            if attempt == registration.WRITE_ATTEMPTS:
                raise HTTPException(status_code=503, detail="database_busy")
            registration.time.sleep(0.3 * attempt)


@bot_router.post("/{approval_uuid}/finalize")
def finalize(approval_uuid: str, payload: FinalizeIn, db: Session = Depends(get_db),
             principal: bot_auth.BotPrincipal = Depends(get_bot_principal)):
    return finalize_approval(db, principal, approval_uuid, payload.failed)


@bot_router.post("/manual")
def register_manual(payload: ApprovalIntentIn, response: Response, db: Session = Depends(get_db),
                    principal: bot_auth.BotPrincipal = Depends(get_bot_principal)):
    body = begin_approval(db, principal, payload, registration.MANUAL)
    response.status_code = 201 if body.get("created") else 200
    return body
