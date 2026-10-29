"""Superadmin provisioning status (real p6_ready(), 7.7) and the explicit control plane."""
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from .. import models_provisioning as mp
from ..database import get_db
from ..deps import require_confirm_password, require_superadmin
from ..services import provisioning_control as control, provisioning_readiness, provisioning_schema
from ..services.remote_action import ActionType
from ..services.remote_runner_registry import ACTIONS

router = APIRouter(prefix="/api/provisioning", tags=["provisioning"],
                   dependencies=[Depends(require_superadmin)])


@router.get("/status")
def status(db: Session = Depends(get_db)):
    # Explicit deployment capability, not "all tables exist => P6 ready".
    # p6_ready is the real 7.7 predicate AND no known implementation gap:
    # a gap can never be outvoted by data that happens to look ready.
    result = dict(schema_ready=provisioning_schema.is_ready(), stage="infrastructure_only",
                  p6_ready=False, receipt_void_execution_available=False,
                  blockers=list(provisioning_readiness.IMPLEMENTATION_GAPS))
    if not result["schema_ready"]:
        result["blockers"].insert(0, "provisioning_schema_not_ready")
        return result  # Do not query unavailable tables or leak inspector details.
    try:
        result.update(_snapshot(db))
        if result["missing_runner_actions"]:
            result["blockers"].append("real_runner_actions_not_registered")
        ready, readiness = provisioning_readiness.p6_ready(db)
        result["readiness_blockers"] = readiness
        result["blockers"].extend(item["code"] for item in readiness)
        result["p6_ready"] = ready and not result["blockers"]
        if result["p6_ready"]:
            result["stage"] = "p6_ready"
    except (SQLAlchemyError, LookupError):
        # Startup readiness can become stale after an external schema change.
        # Never disclose raw SQL/errors or publish a partially-read snapshot.
        db.rollback()
        result["schema_ready"] = False
        result["blockers"].insert(0, "provisioning_status_unavailable")
    return result


def _snapshot(db):
    runtime = db.execute(select(mp.ProvisioningRuntimeState.gate_mode,
        mp.ProvisioningRuntimeState.lock_backend, mp.ProvisioningRuntimeState.owner_state,
        mp.ProvisioningRuntimeState.ownership_epoch).where(mp.ProvisioningRuntimeState.id == 1)).mappings().one_or_none()
    if runtime is None:
        raise LookupError("runtime missing")
    result = {"runtime": dict(runtime)}
    # What a superadmin must run before this host may write through
    # 'enforced' (None: nothing) - after a deploy or a reboot.
    from ..services import provisioning_host_lifecycle as lifecycle
    action = lifecycle.ownership_action(db.get(mp.ProvisioningRuntimeState, 1), lifecycle._me())
    result["runtime"]["ownership_action"] = action
    result["runtime"]["ownership_action_route"] = lifecycle.OWNERSHIP_ACTIONS.get(action)
    result["operation_counts"] = dict(db.execute(select(mp.ProvisioningOperation.state,
        func.count()).group_by(mp.ProvisioningOperation.state)).all())
    result["step_counts"] = dict(db.execute(select(mp.ProvisioningStep.state,
        func.count()).group_by(mp.ProvisioningStep.state)).all())
    result["reservation_counts"] = dict(db.execute(select(mp.PaymentReservation.state,
        func.count()).group_by(mp.PaymentReservation.state)).all())
    result["type_modes"] = dict(db.execute(select(mp.ProvisioningTypeMode.operation_type,
        mp.ProvisioningTypeMode.mode)).all())
    result["node_contract_counts"] = dict(db.execute(select(mp.ProvisioningNodeContract.state,
        func.count()).group_by(mp.ProvisioningNodeContract.state)).all())
    result["missing_runner_actions"] = sorted(action.value for action in ActionType
        if not action.value.startswith("selftest_") and action not in ACTIONS)
    return result


# ------------------------------------------------------------------ control plane (10.4, 10.6, 16)
# Every action is superadmin + the admin's own password (X-Confirm-Password),
# one explicit step, audited in provisioning_ownership_events where the
# design says so. Nothing here is automatic.

def get_session_factory():
    from ..database import SessionLocal
    return SessionLocal


class ReclaimRequest(BaseModel):
    reason: str
    confirmation: str


class TakeoverRequest(BaseModel):
    reason: str
    confirmation: str
    second_confirmation: bool = False


class GateModeRequest(BaseModel):
    target: Literal["off", "shadow", "enforced"]


class TypeModeRequest(BaseModel):
    mode: Literal["legacy", "durable"]


@router.post("/lock-backend/verify")
def verify_lock_backend(admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.verify_claim(factory, admin.id)


@router.post("/ownership/drain")
def ownership_drain(admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.drain(factory, admin.id)


@router.post("/ownership/drain-cancel")
def ownership_drain_cancel(admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.cancel_drain(factory, admin.id)


@router.post("/ownership/release")
def ownership_release(admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.release(factory, admin.id)


@router.post("/ownership/reclaim")
def ownership_reclaim(payload: ReclaimRequest, admin=Depends(require_confirm_password),
                      factory=Depends(get_session_factory)):
    return control.reclaim(factory, admin.id, reason=payload.reason, confirmation=payload.confirmation)


@router.post("/ownership/forced-takeover")
def ownership_forced_takeover(payload: TakeoverRequest, admin=Depends(require_confirm_password),
                              factory=Depends(get_session_factory)):
    return control.forced_takeover(factory, admin.id, reason=payload.reason, confirmation=payload.confirmation,
                                   second_confirmation=payload.second_confirmation)


@router.post("/gate-mode")
def gate_mode(payload: GateModeRequest, admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.change_gate_mode(factory, admin.id, payload.target)


@router.post("/type-modes/{operation_type}")
def type_mode(operation_type: str, payload: TypeModeRequest, admin=Depends(require_confirm_password),
              factory=Depends(get_session_factory)):
    return control.change_type_mode(factory, admin.id, operation_type, payload.mode)


@router.post("/gate-stats/reset")
def gate_stats_reset(admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.reset_gate_stats(factory, admin.id)


@router.post("/nodes/{node_id}/contract/verify")
def verify_node_contract(node_id: int, admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    return control.verify_node_contract(factory, admin.id, node_id)


class ApprovalRecoveryRequest(BaseModel):
    decision: Literal["continue", "cancel"]
    reason: str


@router.post("/approval-recovery/{approval_uuid}")
def approval_recovery(approval_uuid: str, payload: ApprovalRecoveryRequest, admin=Depends(require_confirm_password),
                      factory=Depends(get_session_factory)):
    """An approval whose binding was lost (key revoked/rotated mid-execution)
    or whose shadow manifest did not validate (Lifecycle 7.6): continue ->
    the same T_final with this recovery in place of the key check; cancel ->
    compensation and T_comp. Actor and reason stay on the recovery row."""
    from ..services import provisioning_approval_final as approval_final
    return approval_final.decide(factory, approval_uuid, payload.decision, admin.id, payload.reason)


@router.get("/approval-recovery")
def list_approval_recoveries(db: Session = Depends(get_db)):
    """Open approval recoveries with their operation (no secret, no username)."""
    from .. import models_receipt_void as rv
    ops = rv.wallet_operations
    rows = db.execute(select(ops.c.business_key, ops.c.state, ops.c.created_at).where(
        ops.c.operation_type == "approval_recovery", ops.c.state.in_(("cleanup_required", "running")))
        .order_by(ops.c.id)).all()
    out = []
    for row in rows:
        operation = db.execute(select(mp.ProvisioningOperation.id, mp.ProvisioningOperation.state,
                                      mp.ProvisioningOperation.error_code).where(
            mp.ProvisioningOperation.approval_uuid == row.business_key)
            .order_by(mp.ProvisioningOperation.id.desc())).first()
        import datetime as _dt
        from ..services import provisioning_approval_final as approval_final
        age = (_dt.datetime.utcnow() - row.created_at).days if row.created_at else 0
        out.append({"approval_uuid": row.business_key, "state": row.state, "created_at": row.created_at,
                    "stale": age >= approval_final.WARN_AFTER_DAYS,
                    "continue_allowed": age < approval_final.CONTINUE_CLOSES_AFTER_DAYS,
                    "operation_id": operation.id if operation else None,
                    "operation_state": operation.state if operation else None,
                    "error_code": operation.error_code if operation else None})
    return out


class OperatorRequest(BaseModel):
    reason: str


class RotateRequest(BaseModel):
    reason: str
    fork: bool = False
    confirmation: str = ""


@router.get("/operations/{operation_id}")
def get_operation(operation_id: int, db: Session = Depends(get_db)):
    from ..services import provisioning_operator as operator
    return operator.view(db, operation_id)


@router.post("/operations/{operation_id}/retry")
def retry_operation(operation_id: int, payload: OperatorRequest, admin=Depends(require_confirm_password),
                    factory=Depends(get_session_factory)):
    from ..services import provisioning_operator as operator
    return operator.retry(factory, admin.id, operation_id, payload.reason)


class ForceStepRequest(BaseModel):
    step_id: int
    reason: str


@router.post("/operations/{operation_id}/force-step")
def force_operation_step(operation_id: int, payload: ForceStepRequest, admin=Depends(require_confirm_password),
                         factory=Depends(get_session_factory)):
    """Lifecycle 11.5: one cleanup_required step -> removed / 'abandoned'
    with this admin and reason; then compensation (deletion: its T_final)."""
    from ..services import provisioning_operator as operator
    return operator.force_step(factory, admin.id, operation_id, payload.step_id, payload.reason)


class ManualXrayAbandonRequest(BaseModel):
    step_id: StrictInt
    reason: StrictStr
    confirmation: StrictStr
    acknowledged: StrictBool


@router.post("/operations/{operation_id}/manual-xray-abandon")
def abandon_unknown_xray_delete(operation_id: int, payload: ManualXrayAbandonRequest,
                                admin=Depends(require_confirm_password), factory=Depends(get_session_factory)):
    """Admin-attested resolution for one unknown 3X-UI delete result.

    This records an abandoned/unverified remote outcome, never verified absence.
    """
    from ..services import provisioning_operator as operator
    return operator.abandon_unknown_xray_delete(factory, admin.id, operation_id, payload.step_id,
        payload.reason, payload.confirmation, payload.acknowledged)


@router.get("/orphans")
def list_orphans(db: Session = Depends(get_db)):
    from ..services import provisioning_operator as operator
    return operator.orphans(db)


@router.post("/installation/rotate")
def rotate_installation(payload: RotateRequest, admin=Depends(require_confirm_password),
                        factory=Depends(get_session_factory)):
    from ..services import provisioning_operator as operator
    return operator.rotate(factory, admin.id, reason=payload.reason, fork=payload.fork,
                           confirmation=payload.confirmation)

