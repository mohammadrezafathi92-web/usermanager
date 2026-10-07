"""Private explicit claim/reboot-reclaim CAS; no live ownership/mode API.

Caller authenticates the password/confirmation, performs lock verification,
keeps the exclusive mode lock held through ONE outer commit and rolls back
every failure. Claim never transfers ownership based on heartbeat age.
L0 startup pins remain unchanged until the whole control plane is ready.
"""
import datetime as dt
import json

from fastapi import HTTPException
from sqlalchemy import select, text

from .. import models, models_provisioning as mp
from . import gate_locks, provisioning_schema, provisioning_transitions
from .provisioning_lock_verification import VerifiedLocks, require_exclusive

RECLAIM_CONFIRMATION = "process قبلی این نصب دیگر در حال اجرا نیست"


def _ready(db, proof, mode_hold, actor_admin_id, version, base_dir):
    provisioning_schema.assert_ready()
    if db.get_transaction() is None:
        raise HTTPException(409, "ownership_transaction_required")
    if type(actor_admin_id) is not int or actor_admin_id <= 0 or type(version) is not int or version < 0:
        raise HTTPException(422, "ownership_request_invalid")
    actor = db.get(models.AdminUser, actor_admin_id)
    if actor is None or not actor.is_superadmin:
        raise HTTPException(403, "ownership_superadmin_required")
    if not isinstance(proof, VerifiedLocks) or not isinstance(proof.checked_at, dt.datetime):
        raise HTTPException(409, "ownership_lock_not_ready")
    try:
        age = (dt.datetime.utcnow() - proof.checked_at).total_seconds()
    except (TypeError, ValueError):
        raise HTTPException(409, "ownership_lock_not_ready") from None
    expected_backend = "flock" if db.get_bind().dialect.name == "sqlite" else "flock+get_lock"
    if not 0 <= age <= 60 or proof.backend != expected_backend:
        raise HTTPException(409, "ownership_lock_not_ready")
    require_exclusive(mode_hold, proof.installation_uuid, base_dir)
    row = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
        .with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None or row.installation_uuid != proof.installation_uuid or row.version != version or row.ownership_epoch < 0:
        raise HTTPException(409, "ownership_held")
    panel = db.get(models.PanelSettings, 1)
    if panel is not None and panel.ha_enabled:
        raise HTTPException(409, "provisioning_ha_unsupported")
    return row


def _clock(db):
    if db.get_bind().dialect.name == "sqlite":
        return dt.datetime.fromisoformat(db.execute(text("SELECT strftime('%Y-%m-%d %H:%M:%f','now')")).scalar_one())
    return db.execute(text("SELECT NOW(6)")).scalar_one()


def _write(db, row, values, event_type, actor_admin_id, reason=None):
    previous_host = row.owner_host_id
    result = db.execute(mp.ProvisioningRuntimeState.__table__.update().where(
        mp.ProvisioningRuntimeState.id == 1, mp.ProvisioningRuntimeState.version == row.version,
        mp.ProvisioningRuntimeState.owner_state == row.owner_state,
    ).values(**values, version=mp.ProvisioningRuntimeState.version + 1))
    if result.rowcount != 1:
        raise HTTPException(409, "ownership_held")
    db.add(mp.ProvisioningOwnershipEvent(event_type=event_type, from_host_id=previous_host,
        to_host_id=values["owner_host_id"], ownership_epoch=values["ownership_epoch"],
        actor_admin_id=actor_admin_id, reason=reason))
    db.flush()
    db.expire(row)
    return row


def claim(db, proof, version, mode_hold, *, actor_admin_id, base_dir=None):
    row = _ready(db, proof, mode_hold, actor_admin_id, version, base_dir)
    same = row.owner_state == "active" and (row.owner_host_id, row.owner_boot_id) == (
        proof.identity.host_id, proof.identity.boot_id)
    vacant = row.owner_state in ("none", "released") and row.owner_host_id is None
    if not (same or vacant):
        raise HTTPException(409, "ownership_held")
    now = _clock(db)
    values = dict(owner_state="active", owner_host_id=proof.identity.host_id, owner_boot_id=proof.identity.boot_id,
        owner_claimed_at=now if vacant else row.owner_claimed_at, owner_heartbeat_at=now,
        ownership_epoch=row.ownership_epoch + int(vacant), lock_backend=proof.backend,
        lock_verified_at=now, changed_at=now)
    return _write(db, row, values, "claim" if vacant else "verify", actor_admin_id)


def reclaim(db, proof, version, mode_hold, *, actor_admin_id, reason, confirmation, base_dir=None):
    # The route (not present yet) must also verify the superadmin password.
    # An explicit phrase/reason is not proof of the former process's death.
    if confirmation != RECLAIM_CONFIRMATION or not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise HTTPException(422, "ownership_reclaim_confirmation_required")
    row = _ready(db, proof, mode_hold, actor_admin_id, version, base_dir)
    if row.owner_state not in ("active", "draining") or row.owner_host_id != proof.identity.host_id or (
            row.owner_boot_id == proof.identity.boot_id):
        raise HTTPException(409, "ownership_reclaim_invalid")
    audit = json.dumps(dict(reason=reason.strip(), from_boot_id=row.owner_boot_id,
        to_boot_id=proof.identity.boot_id), ensure_ascii=False, separators=(",", ":"))
    if len(audit) > 500:
        raise HTTPException(422, "ownership_reclaim_reason_too_long")
    now = _clock(db)
    values = dict(owner_state="active", owner_host_id=proof.identity.host_id, owner_boot_id=proof.identity.boot_id,
        owner_claimed_at=now, owner_heartbeat_at=now, ownership_epoch=row.ownership_epoch + 1,
        lock_backend=proof.backend, lock_verified_at=now, changed_at=now)
    return _write(db, row, values, "reclaim", actor_admin_id, audit)


def _same(row, proof, state):
    if row.owner_state != state or (row.owner_host_id, row.owner_boot_id) != (
            proof.identity.host_id, proof.identity.boot_id):
        raise HTTPException(409, "ownership_held")


def drain(db, proof, version, mode_hold, *, actor_admin_id, base_dir=None):
    row = _ready(db, proof, mode_hold, actor_admin_id, version, base_dir)
    _same(row, proof, "active")
    return _write(db, row, dict(owner_state="draining", owner_host_id=row.owner_host_id,
        ownership_epoch=row.ownership_epoch, changed_at=_clock(db)), "drain_start", actor_admin_id)


def cancel_drain(db, proof, version, mode_hold, *, actor_admin_id, base_dir=None):
    row = _ready(db, proof, mode_hold, actor_admin_id, version, base_dir)
    _same(row, proof, "draining")
    return _write(db, row, dict(owner_state="active", owner_host_id=row.owner_host_id,
        ownership_epoch=row.ownership_epoch, changed_at=_clock(db)), "drain_cancel", actor_admin_id)


def release(db, proof, version, mode_hold, node_holds, *, actor_admin_id, base_dir=None):
    """Caller holds mode EX and every saved node's EX lock until commit.

    The all-node set is checked again inside the transaction. No bool/timeout
    or old heartbeat can substitute for a held lock or terminal operations.
    """
    row = _ready(db, proof, mode_hold, actor_admin_id, version, base_dir)
    _same(row, proof, "draining")
    if row.gate_mode == "enforced":
        # Frozen ck_provrt_enforced_needs_owner only permits active/draining.
        # Do not trigger a raw CHECK failure or silently relax the gate. A
        # controlled constraint upgrade is needed before enforced handoff.
        raise HTTPException(503, "ownership_release_protocol_unavailable")
    pending = db.execute(select(mp.ProvisioningOperation.id).where(
        mp.ProvisioningOperation.state.notin_(provisioning_transitions.TERMINAL))).scalars().all()
    if pending:
        raise HTTPException(409, dict(code="ownership_operations_pending", operation_ids=pending))
    nodes = set(db.execute(select(models.Node.id)).scalars())
    if not isinstance(node_holds, dict) or set(node_holds) != nodes or any(
            not isinstance(hold, gate_locks.FileLock) or not hold.held or hold.shared is not False or
            hold.path != gate_locks.node_lock_path(proof.installation_uuid, node_id, base_dir)
            for node_id, hold in node_holds.items()):
        raise HTTPException(409, "ownership_nodes_not_quiescent")
    now = _clock(db)
    return _write(db, row, dict(owner_state="released", owner_host_id=None, owner_boot_id=None,
        owner_claimed_at=None, owner_heartbeat_at=None, ownership_epoch=row.ownership_epoch + 1,
        # An enforced gate cannot have a vacant owner according to its CHECK.
        # Releasing ownership must not silently lower the gate to shadow/off.
        changed_at=now), "release", actor_admin_id)
