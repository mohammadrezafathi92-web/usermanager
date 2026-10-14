"""Host-side ownership housekeeping (Lifecycle 10.4).

heartbeat()         every 60 s: owner_heartbeat_at = now, only WHERE the
                    owner is exactly this (host, boot). Display and warning
                    only - no path ever moves ownership because a heartbeat
                    is old, and it does not bump the CAS version.
shutdown_release()  on a clean shutdown: no new runner, every live runner
                    finished or killed and reaped, then ONE CAS that writes
                    owner_state='released', ownership_epoch+1 and a
                    'shutdown_release' event (actor 0 = system). Correctness
                    never depends on it: without it the next boot needs an
                    explicit reclaim. With the gate 'enforced' it releases
                    only if the installed ownership CHECK allows a released
                    owner (provisioning_runtime_contract); otherwise the row
                    is left as it is.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select, update

from .. import models_provisioning as mp
from . import provisioning_runtime_contract, provisioning_schema
from .provisioning_host import HostIdentityUnavailable, read_identity

logger = logging.getLogger(__name__)
HEARTBEAT_SECONDS = 60


def _me():
    try:
        return read_identity()
    except HostIdentityUnavailable:
        return None


def heartbeat(session_factory=None) -> bool:
    if not provisioning_schema.is_ready():
        return False
    identity = _me()
    if identity is None:
        return False
    if session_factory is None:
        from ..database import SessionLocal as session_factory  # noqa: N813
    try:
        with session_factory() as db:
            table = mp.ProvisioningRuntimeState.__table__
            result = db.execute(update(table).where(
                table.c.id == 1, table.c.owner_state.in_(("active", "draining")),
                table.c.owner_host_id == identity.host_id, table.c.owner_boot_id == identity.boot_id)
                .values(owner_heartbeat_at=dt.datetime.utcnow()))
            db.commit()
            return result.rowcount == 1
    except Exception:  # noqa: BLE001 - display only; never disturbs the process
        logger.exception("provisioning heartbeat failed")
        return False


def shutdown_release(session_factory=None, *, grace_seconds: float = 5.0) -> str:
    """Returns what happened: 'released', 'not_owner', 'kept' or 'unavailable'."""
    from . import remote_runner
    killed = remote_runner.shutdown(grace_seconds)
    if killed:
        logger.warning("shutdown: %s runner(s) killed and reaped before releasing ownership", killed)
    if not provisioning_schema.is_ready():
        return "unavailable"
    identity = _me()
    if identity is None:
        return "unavailable"
    if session_factory is None:
        from ..database import SessionLocal as session_factory  # noqa: N813
    try:
        with session_factory() as db:
            row = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
                             .with_for_update()).scalar_one_or_none()
            if row is None or row.owner_state not in ("active", "draining") or (
                    row.owner_host_id, row.owner_boot_id) != (identity.host_id, identity.boot_id):
                db.rollback()
                return "not_owner"
            if row.gate_mode == "enforced" and not provisioning_runtime_contract.supports_release(db.connection()):
                db.rollback()
                return "kept"
            table = mp.ProvisioningRuntimeState.__table__
            now = dt.datetime.utcnow()
            result = db.execute(update(table).where(table.c.id == 1, table.c.version == row.version).values(
                owner_state="released", owner_host_id=None, owner_boot_id=None, owner_claimed_at=None,
                owner_heartbeat_at=None, ownership_epoch=row.ownership_epoch + 1, changed_at=now,
                version=table.c.version + 1))
            if result.rowcount != 1:
                db.rollback()
                return "not_owner"
            db.add(mp.ProvisioningOwnershipEvent(event_type="shutdown_release", from_host_id=identity.host_id,
                                                 to_host_id=None, ownership_epoch=row.ownership_epoch + 1,
                                                 actor_admin_id=0))
            db.commit()
            return "released"
    except Exception:  # noqa: BLE001
        logger.exception("shutdown ownership release failed; the next boot needs an explicit reclaim")
        return "unavailable"
