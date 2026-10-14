"""Read-only superadmin provisioning status and the real p6_ready() (7.7)."""
from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from .. import models_provisioning as mp
from ..database import get_db
from ..deps import require_superadmin
from ..services import provisioning_readiness, provisioning_schema
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
