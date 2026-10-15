"""HA has no external fencing (Receipt Void 20.4): enabling it is refused
while anything that needs exactly-once state is on - the wallet's lot
generation (or its 'fencing' phase) and required approval registration."""
from fastapi import HTTPException
from sqlalchemy import select

from .. import models_receipt_void as rv
from . import receipt_void_schema


def refuse_enabling(db) -> None:
    if not receipt_void_schema.is_ready():
        return
    from . import wallet_service
    wallet_service.lock_phase_for_change(db)        # serialized with the cutover, held to this commit
    wallet = db.execute(select(rv.wallet_runtime_state.c.phase, rv.wallet_runtime_state.c.external_fencing).where(
        rv.wallet_runtime_state.c.id == 1)).one_or_none()
    approval = db.execute(select(rv.receipt_approval_runtime_state.c.registration_mode,
                                 rv.receipt_approval_runtime_state.c.auto_rate_limit_mode).where(
        rv.receipt_approval_runtime_state.c.id == 1)).one_or_none()
    if (wallet is not None and wallet.phase != "normal" and wallet.external_fencing != "coordinator") or (
            approval is not None and (approval.registration_mode == "required"
                                      or approval.auto_rate_limit_mode == "enforced")):
        raise HTTPException(409, "ha_requires_external_fencing")
