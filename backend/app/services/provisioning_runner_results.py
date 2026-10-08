"""Parent-only short transaction for a completed child result; no live caller.

Call only AFTER the runner has exited and released its node gates. Caller
begins the fenced writer transaction, owns current resource leases, rolls
back on failure and commits once. No remote I/O, capture, refund or terminal
operation finalization occurs here. Unknown results require a recovery read.
"""
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import select

from .. import models_provisioning as mp
from . import resource_leases, provisioning_schema, provisioning_transitions as transitions
from .adapter_base import AbsentOutcome, PresentResult, ReadResult, ReadState
from .provisioning_dispatch_binding import DispatchBinding
from .provisioning_host import HostIdentity
from . import remote_action

RemoteActionResult = remote_action.RemoteActionResult
RemoteActionError = remote_action.RemoteActionError
Outcome = remote_action.Outcome
SCHEMA_VERSION = remote_action.SCHEMA_VERSION


def record(db, binding, identity, leases, expected_action_id, result, *, recovery_read=False):
    if not isinstance(binding, DispatchBinding) or not isinstance(identity, HostIdentity) or (
            not isinstance(result, RemoteActionResult) or type(recovery_read) is not bool):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    try:
        result = RemoteActionResult.from_wire(result.to_wire())
    except (RemoteActionError, TypeError, ValueError):
        raise HTTPException(422, "provisioning_runner_result_invalid") from None
    if result.action_id != expected_action_id or result.schema_version != SCHEMA_VERSION:
        raise HTTPException(409, "provisioning_runner_result_mismatch")
    forward, deleting = binding.phase == "forward", binding.phase == "deletion"
    if binding.phase not in ("forward", "compensation", "deletion"):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    convergence = result.error_code == "remote_convergence_required"
    if convergence and (not forward or not recovery_read or result.outcome != Outcome.UNREADABLE or result.write_attempted):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    # Parent-generated kill/timeout flags are conservative, not observed
    # writes. They can record an unknown read attempt, but can never certify
    # success/absence/convergence or release anything.
    if ((recovery_read and not result.is_unknown) or result.outcome == Outcome.UNREADABLE) and result.write_attempted:
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if result.outcome in (Outcome.SUCCEEDED, Outcome.ALREADY_PRESENT_VERIFIED) and (
            not forward or result.remote_outcome is not None or
            (result.outcome == Outcome.SUCCEEDED and not result.write_attempted) or
            (result.outcome == Outcome.ALREADY_PRESENT_VERIFIED and result.write_attempted)):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if result.outcome == Outcome.ABSENT_VERIFIED and (
            result.remote_outcome not in ("verified_absent", "delete_idempotently_absent") or
            (forward and not recovery_read)):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if result.outcome == Outcome.CONFLICT and result.write_attempted:
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if result.outcome == Outcome.CONFLICT and result.remote_outcome not in (None, "unverified"):
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if result.outcome not in (Outcome.ABSENT_VERIFIED, Outcome.CONFLICT) and result.remote_outcome is not None:
        raise HTTPException(422, "provisioning_runner_result_invalid")
    if db.get_transaction() is None or db.info.get("resource_lease_business_transaction") is not db.get_transaction():
        raise HTTPException(409, "provisioning_result_transaction_not_fenced")
    provisioning_schema.assert_ready()
    runtime = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if runtime is None or (runtime.installation_uuid, runtime.owner_host_id, runtime.owner_boot_id,
            runtime.ownership_epoch, runtime.gate_mode, runtime.gate_mode_epoch) != (
            binding.installation_uuid, identity.host_id, identity.boot_id, binding.ownership_epoch,
            "enforced", binding.gate_mode_epoch) or runtime.owner_state not in ("active", "draining"):
        raise HTTPException(409, "provisioning_result_owner_changed")
    tokens = tuple(leases)
    if not tokens or any(not isinstance(token, resource_leases.Lease) or token.owner != binding.lease_owner
                        for token in tokens) or not any((token.resource_key, token.epoch) == (
                        f"provisioning_op:{binding.operation_id}", binding.lease_epoch) for token in tokens):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    resource_leases.revalidate(db, tokens)
    operation, step = transitions._step(db, binding.step_id, binding.step_version)
    expected_direction = "remove" if deleting else "create"
    valid_kinds = (("delete_user", "delete_purchase", "delete_connection") if deleting else
                   ("create_user", "purchase", "add_connection"))
    valid_states = (("provisioning",) if forward or deleting else ("compensating", "cleanup_required"))
    expected_step = "remote_calling" if forward or deleting else "compensating"
    if (operation.id, operation.version, step.node_id, step.backend, step.direction) != (
            binding.operation_id, binding.operation_version, binding.node_id, binding.backend, expected_direction) or (
            operation.operation_type not in valid_kinds) or operation.state not in valid_states or (
            step.state != expected_step) or not step.remote_attempted:
        raise HTTPException(409, "provisioning_runner_result_stale")
    if forward and result.outcome in (Outcome.SUCCEEDED, Outcome.ALREADY_PRESENT_VERIFIED):
        return transitions.confirm_present(db, step.id, step.version,
            PresentResult(result.outcome == Outcome.SUCCEEDED))
    if convergence:
        # A positively matching identity needs remaining side effects. Keep
        # remote_attempted and credentials; the next CAS dispatch re-reads
        # identity under fresh child authority before converging, not creating
        # blindly. Old result/dispatch versions are no longer usable.
        return transitions._write_step(db, step, "staged", error_code="remote_convergence_required", next_retry_at=None)
    if result.outcome == Outcome.ABSENT_VERIFIED:
        if deleting:
            return transitions.confirm_removed(db, step.id, step.version, AbsentOutcome(result.remote_outcome))
        if forward:
            return transitions.recover_read(db, step.id, step.version, ReadResult(ReadState.ABSENT))
        return transitions.confirm_absent(db, step.id, step.version, AbsentOutcome(result.remote_outcome))
    if result.outcome == Outcome.CONFLICT:
        if deleting:
            return transitions.confirm_removed(db, step.id, step.version, AbsentOutcome.UNVERIFIED)
        if forward:
            return transitions.recover_read(db, step.id, step.version, ReadResult(ReadState.PRESENT_CONFLICT))
        return transitions.confirm_absent(db, step.id, step.version, AbsentOutcome.UNVERIFIED)
    # Never turn a kill/timeout/possibly-attempted write into an absence,
    # compensated operation, released reservation or financial refund.
    error = "remote_result_unknown" if result.is_unknown else (
        "remote_unreadable" if result.outcome == Outcome.UNREADABLE else "remote_transport_error")
    return transitions._write_step(db, step, step.state, error_code=error,
        next_retry_at=dt.datetime.utcnow() + dt.timedelta(seconds=30))
