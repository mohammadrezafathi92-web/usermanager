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
    sqlite = db.get_bind().dialect.name == "sqlite"
    # Current (locking) reads after the lock - never the transaction's
    # older snapshot (MariaDB): a cutover that committed while this waited
    # is seen.
    wallet = select(rv.wallet_runtime_state.c.phase, rv.wallet_runtime_state.c.external_fencing).where(
        rv.wallet_runtime_state.c.id == 1)
    approval = select(rv.receipt_approval_runtime_state.c.registration_mode,
                      rv.receipt_approval_runtime_state.c.auto_rate_limit_mode).where(
        rv.receipt_approval_runtime_state.c.id == 1)
    if not sqlite:
        wallet, approval = wallet.with_for_update(), approval.with_for_update(read=True)
    wallet = db.execute(wallet).one_or_none()
    approval = db.execute(approval).one_or_none()
    if (wallet is not None and wallet.phase != "normal" and wallet.external_fencing != "coordinator") or (
            approval is not None and (approval.registration_mode == "required"
                                      or approval.auto_rate_limit_mode == "enforced")):
        raise HTTPException(409, "ha_requires_external_fencing")
