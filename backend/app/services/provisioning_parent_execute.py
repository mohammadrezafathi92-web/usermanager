"""One private parent dispatch/result cycle; no scheduler/API/live caller.

Caller owns leases. The first short transaction commits the dispatch marker
before spawning. The session closes before remote I/O. A second fresh fenced
transaction records only a matching result. No capture, delivery, operation
finalization, reservation release or mode activation occurs here.
"""
import os

from fastapi import HTTPException
from sqlalchemy.engine import make_url

from . import provisioning_parent_dispatch as dispatch, provisioning_runner_results as results
from . import provisioning_transitions as transitions, resource_leases, remote_runner, remote_action
from .provisioning_dispatch_binding import DispatchBinding


def _configured_source(expected_url):
    try:
        configured = make_url(os.environ.get("DATABASE_URL", ""))
        if configured != expected_url:
            raise ValueError()
    except Exception:
        raise HTTPException(503, "provisioning_runner_configuration_mismatch") from None


def execute_one(session_factory, step_id, version, identity, leases, *, recovery_read, expected_owner=None):
    return _execute(session_factory, step_id, version, identity, leases,
        recovery_read=recovery_read, compensation=False, expected_owner=expected_owner)


def execute_compensation(session_factory, step_id, version, identity, leases, *, expected_owner=None):
    """Private cycle for guarded absence actions; no live caller.

    This records only a cleanup step result, never a reservation release,
    operation finalization or refund. Caller owns and retains its leases.
    """
    return _execute(session_factory, step_id, version, identity, leases,
        recovery_read=False, compensation=True, expected_owner=expected_owner)


def _execute(session_factory, step_id, version, identity, leases, *, recovery_read, compensation, expected_owner):
    tokens = tuple(leases)
    with session_factory() as db:
        expected_url = db.get_bind().url
        _configured_source(expected_url)
        resource_leases.begin_business(db)
        dto = (dispatch.compensation_snapshot(db, step_id, version, identity, tokens) if compensation else
            dispatch.snapshot(db, step_id, version, identity, tokens, recovery_read=recovery_read))
        if expected_owner is not None and (type(expected_owner) is not tuple or len(expected_owner) != 2 or
                type(expected_owner[1]) is not int or (dto.fencing["installation_uuid"],
                dto.fencing["binding"]["ownership_epoch"]) != expected_owner):
            raise HTTPException(409, "provisioning_dispatch_owner_changed")
        db.commit()
    # No application DB session/transaction survives into the remote call.
    _configured_source(expected_url)
    try:
        result = remote_runner.run_action(dto)
    except Exception:
        # A launcher/transport failure must not be treated as absence or
        # refund evidence. run_action kills/waits for any child it started.
        result = remote_action.RemoteActionResult(dto.action_id, remote_action.Outcome.TRANSPORT_ERROR,
            write_attempted=not recovery_read, error_code="runner_transport_unknown")
    binding = DispatchBinding(**dto.fencing["binding"])
    with session_factory() as db:
        resource_leases.begin_business(db)
        row = results.record(db, binding, identity, tokens, dto.action_id, result, recovery_read=recovery_read)
        public = transitions.public_step(row)
        db.commit()
    return public
