"""Superadmin control plane of durable provisioning (Lifecycle 10.4, 10.6, 16).

Each action here is one explicit, audited step; nothing is automatic and no
heartbeat age ever transfers ownership. The ownership cores themselves are
services/provisioning_ownership.py; this module supplies what they require
from their caller - fresh host identity, the installation namespace check,
the exclusive gate-mode lock held through ONE commit, and the lock
self-tests - and adds the two mode switches the design defines:

  change_gate_mode   off <-> shadow <-> enforced (10.6 table of preconditions)
  change_type_mode   one operation_type legacy <-> durable (10.6, 15, 16)

Fail-closed rules of this build:
  * 'enforced' cannot be entered while node_gate itself cannot run legacy
    writers in the runner (node_gate.ENFORCED_SUPPORTED is False): entering
    it would stop the poller and every legacy writer.
  * a type cannot become durable while its production callers are not
    wired to the durable path (DURABLE_WIRED), whatever the data says.
"""
from __future__ import annotations

import contextlib
import datetime as dt

from fastapi import HTTPException
from sqlalchemy import func, select, text

from .. import models, models_provisioning as mp
from . import gate_locks, node_gate, provisioning_installation, provisioning_lock_verification as verification
from . import provisioning_ownership as ownership, provisioning_readiness as readiness, provisioning_schema
from .provisioning_host import HostIdentityUnavailable, read_identity
from .provisioning_transitions import TERMINAL

ACTIVATION_TIMEOUT_SECONDS = 60          # Lifecycle 18 decision 14 (default)
GATE_MODES = ("off", "shadow", "enforced")

# Operation types whose every production caller runs the durable path when
# the type is durable. Grows only with the change that wires a type's callers.
# P6: every type is wired - bot, remote bridge (/api/bot), panel, Mini App,
# wallet purchase and receipt approvals run the durable path when the type is
# durable (renew_user has no production caller of its own: the bot's
# user-level renew URL renews the one service through renew_purchase).
DURABLE_WIRED: frozenset[str] = frozenset(mp.OPERATION_TYPES)


def _identity():
    try:
        return read_identity()
    except HostIdentityUnavailable:
        raise HTTPException(409, "host_identity_unavailable") from None


def _installation(engine, base_dir=None):
    try:
        return provisioning_installation.require_match(engine, base_dir=base_dir)
    except provisioning_installation.InstallationUnavailable as exc:
        raise HTTPException(409, str(exc) or "installation_mismatch") from None


@contextlib.contextmanager
def exclusive_mode_lock(installation_uuid, *, timeout=ACTIVATION_TIMEOUT_SECONDS, base_dir=None):
    """LOCK_EX on the gate-mode file: waits for every writer that started in
    the previous mode (each holds LOCK_SH) to finish, in every process."""
    hold = gate_locks.FileLock(gate_locks.mode_lock_path(installation_uuid, base_dir))
    try:
        hold.acquire(shared=False, timeout=timeout)
    except gate_locks.LockTimeout:
        raise HTTPException(409, "gate_mode_change_timeout") from None
    except gate_locks.LockUnavailable:
        raise HTTPException(409, "ownership_lock_not_ready") from None
    try:
        yield hold
    finally:
        hold.release()
        node_gate.reset_cache()


def _proof(engine, identity, installation_uuid, hold, base_dir):
    try:
        return verification.verify(engine, identity, installation_uuid, hold, base_dir=base_dir)
    except verification.VerificationFailed as exc:
        raise HTTPException(409, dict(code="ownership_lock_not_ready", reason=str(exc))) from None


def _runtime_version(session_factory):
    with session_factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        if row is None:
            raise HTTPException(409, "provisioning_runtime_missing")
        return row.version


def _stored_installation(session_factory):
    """The column is the reference for the namespace (10.10)."""
    with session_factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        if row is None:
            raise HTTPException(409, "provisioning_runtime_missing")
        return db.get_bind(), row.installation_uuid


def _ownership(session_factory, admin_id, action, *, base_dir=None, **kwargs):
    provisioning_schema.assert_ready()
    identity = _identity()
    engine, installation_uuid = _stored_installation(session_factory)
    with exclusive_mode_lock(installation_uuid, base_dir=base_dir) as hold:
        if action is ownership.claim:
            # First claim after L0: the file is written from the column, only
            # while the installation is still inert (10.10, initialize_off).
            try:
                provisioning_installation.read_file(base_dir)
            except provisioning_installation.InstallationUnavailable as exc:
                if str(exc) != "installation_file_missing":
                    raise HTTPException(409, str(exc)) from None
                try:
                    provisioning_installation.initialize_off(engine, installation_uuid, hold, base_dir=base_dir)
                except provisioning_installation.InstallationUnavailable as setup:
                    raise HTTPException(409, str(setup)) from None
        if _installation(engine, base_dir) != installation_uuid:
            raise HTTPException(409, "installation_mismatch")
        proof = _proof(engine, identity, installation_uuid, hold, base_dir)
        version = _runtime_version(session_factory)
        node_holds = {}
        try:
            if action is ownership.release:
                # 10.4 step 2: every node's lock must be free NOW and stay held
                # by us through the commit, so no writer can start meanwhile.
                with session_factory() as db:
                    node_ids = sorted(db.execute(select(models.Node.id)).scalars())
                for node_id in node_ids:
                    lock = gate_locks.FileLock(gate_locks.node_lock_path(installation_uuid, node_id, base_dir))
                    try:
                        node_holds[node_id] = lock.acquire(shared=False, timeout=0)
                    except (gate_locks.LockTimeout, gate_locks.LockUnavailable):
                        raise HTTPException(409, dict(code="ownership_nodes_not_quiescent", node_id=node_id)) from None
            with session_factory() as db:
                try:
                    # The ownership cores require their ONE transaction to be
                    # open (and on SQLite to be the writer) before they read.
                    if db.get_bind().dialect.name == "sqlite":
                        db.execute(text("BEGIN IMMEDIATE"))
                    else:
                        db.begin()
                    if action is ownership.release:
                        action(db, proof, version, hold, node_holds, actor_admin_id=admin_id, base_dir=base_dir)
                    else:
                        action(db, proof, version, hold, actor_admin_id=admin_id, base_dir=base_dir, **kwargs)
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
        finally:
            for lock in node_holds.values():
                lock.release()
    return status_runtime(session_factory)


def verify_claim(session_factory, admin_id, *, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.claim, base_dir=base_dir)


def drain(session_factory, admin_id, *, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.drain, base_dir=base_dir)


def cancel_drain(session_factory, admin_id, *, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.cancel_drain, base_dir=base_dir)


def release(session_factory, admin_id, *, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.release, base_dir=base_dir)


def reclaim(session_factory, admin_id, *, reason, confirmation, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.reclaim, base_dir=base_dir,
                      reason=reason, confirmation=confirmation)


def forced_takeover(session_factory, admin_id, *, reason, confirmation, second_confirmation, base_dir=None):
    return _ownership(session_factory, admin_id, ownership.forced_takeover, base_dir=base_dir,
                      reason=reason, confirmation=confirmation, second_confirmation=second_confirmation)


def status_runtime(session_factory):
    """Non-secret runtime fields for the response (no host ids, no boot ids)."""
    with session_factory() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        return dict(gate_mode=row.gate_mode, gate_mode_epoch=row.gate_mode_epoch, owner_state=row.owner_state,
                    ownership_epoch=row.ownership_epoch, lock_backend=row.lock_backend, version=row.version)


# ------------------------------------------------------------------ gate mode (10.6)
def _stat_totals(db):
    """The two blocker counters, every process's committed counts included.
    This process's pending counts are flushed first - a flush that fails
    stops the change - and the totals are read in a NEW transaction: the
    caller's transaction may already hold a snapshot (MariaDB repeatable
    read, SQLite WAL) older than that flush or than another process's."""
    from sqlalchemy.orm import sessionmaker
    if not node_gate.flush_counters():
        raise HTTPException(409, "gate_counters_not_persisted")
    with sessionmaker(bind=db.get_bind(), autoflush=False)() as fresh:
        row = fresh.execute(select(func.coalesce(func.sum(mp.ProvisioningGateStat.writer_not_instrumented_count), 0),
                                   func.coalesce(func.sum(mp.ProvisioningGateStat.mode_lock_miss_count), 0))).one()
    return int(row[0]), int(row[1])


def _gate_preconditions(db, current, target, identity, installation_uuid, base_dir):
    """The 10.6 table. Raises the first unmet precondition; reads only."""
    row = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)
                     .execution_options(populate_existing=True)).scalar_one()
    if row.gate_mode != current:
        raise HTTPException(409, "gate_mode_changed")
    order = GATE_MODES.index
    if order(target) > order(current):
        if order(target) - order(current) != 1:
            raise HTTPException(409, "gate_mode_transition_invalid")
        if row.lock_backend == "none":
            raise HTTPException(409, "ownership_lock_not_ready")
        if row.owner_state != "active" or (row.owner_host_id, row.owner_boot_id) != (identity.host_id, identity.boot_id):
            raise HTTPException(409, "ownership_not_active")
        missing, misses = _stat_totals(db)
        if misses:
            raise HTTPException(409, dict(code="mode_lock_miss_count_nonzero", count=misses))
        if target == "enforced":
            if missing:
                raise HTTPException(409, dict(code="writer_not_instrumented_count_nonzero", count=missing))
            if not node_gate.ENFORCED_SUPPORTED:
                raise HTTPException(409, "gate_enforced_not_implemented")
            selftest()
        return row
    # Lowering: everything legacy, nothing in flight, nobody holds a node lock.
    durable = sorted(kind for kind, mode in db.execute(select(mp.ProvisioningTypeMode.operation_type,
                     mp.ProvisioningTypeMode.mode)).all() if mode == "durable")
    if durable:
        raise HTTPException(409, dict(code="operation_types_durable", items=durable))
    pending = db.execute(select(mp.ProvisioningOperation.id).where(
        mp.ProvisioningOperation.state.notin_(TERMINAL)).order_by(mp.ProvisioningOperation.id)).scalars().all()
    if pending:
        raise HTTPException(409, dict(code="provisioning_operations_pending", operation_ids=pending))
    for node_id in db.execute(select(models.Node.id).order_by(models.Node.id)).scalars():
        lock = gate_locks.FileLock(gate_locks.node_lock_path(installation_uuid, node_id, base_dir))
        try:
            lock.acquire(shared=False, timeout=0)
        except gate_locks.LockTimeout:
            raise HTTPException(409, dict(code="node_lock_held", node_id=node_id)) from None
        except gate_locks.LockUnavailable:
            pass  # No file: nobody can hold it.
        finally:
            lock.release()
    return row


def selftest():
    """Runner self-test (10.8): spawn, round-trip, clean exit. No node, no network."""
    from . import remote_action, remote_runner
    dto = remote_action.RemoteActionDTO(action_type=remote_action.ActionType.SELFTEST_ECHO, params={"echo": "ok"},
                                        timeouts={"hard_deadline": 30})
    try:
        result = remote_runner.run_action(dto)
    except Exception:  # noqa: BLE001
        raise HTTPException(409, "runner_selftest_failed") from None
    if result.outcome != remote_action.Outcome.SUCCEEDED or result.observed.get("params") != {"echo": "ok"}:
        raise HTTPException(409, "runner_selftest_failed")


def change_gate_mode(session_factory, admin_id, target, *, base_dir=None, timeout=ACTIVATION_TIMEOUT_SECONDS):
    provisioning_schema.assert_ready()
    if target not in GATE_MODES:
        raise HTTPException(422, "gate_mode_invalid")
    identity = _identity()
    with session_factory() as db:
        engine = db.get_bind()
        current = db.get(mp.ProvisioningRuntimeState, 1).gate_mode
    if current == target:
        return status_runtime(session_factory)
    installation_uuid = _installation(engine, base_dir)
    with session_factory() as db:
        _gate_preconditions(db, current, target, identity, installation_uuid, base_dir)   # 1: before waiting
    with exclusive_mode_lock(installation_uuid, timeout=timeout, base_dir=base_dir):     # 2: writers drained
        with session_factory() as db:
            committed = False
            try:
                row = _gate_preconditions(db, current, target, identity, installation_uuid, base_dir)  # 3: again
                # Writers that start after this lock is released compare their
                # cached epoch with this file: "changing" forces a fresh read.
                gate_locks.write_mode_state(installation_uuid, gate_locks.MODE_STATE_CHANGING, base_dir)
                now = ownership._clock(db)
                result = db.execute(mp.ProvisioningRuntimeState.__table__.update().where(
                    mp.ProvisioningRuntimeState.id == 1, mp.ProvisioningRuntimeState.version == row.version,
                    mp.ProvisioningRuntimeState.gate_mode == current).values(
                    gate_mode=target, gate_mode_epoch=mp.ProvisioningRuntimeState.gate_mode_epoch + 1,
                    gate_mode_changed_at=now, changed_at=now, version=mp.ProvisioningRuntimeState.version + 1))
                if result.rowcount != 1:
                    raise HTTPException(409, "gate_mode_changed")
                db.add(mp.ProvisioningOwnershipEvent(event_type="gate_mode_change", from_host_id=row.owner_host_id,
                    to_host_id=row.owner_host_id, ownership_epoch=row.ownership_epoch, gate_mode_from=current,
                    gate_mode_to=target, actor_admin_id=admin_id))
                db.commit()
                committed = True
                gate_locks.write_mode_state(installation_uuid, str(row.gate_mode_epoch + 1), base_dir)
            except Exception:
                if not committed:
                    db.rollback()
                raise
    return status_runtime(session_factory)


# ------------------------------------------------------------------ type mode (10.6, 15, 16)
def change_type_mode(session_factory, admin_id, operation_type, target):
    provisioning_schema.assert_ready()
    if operation_type not in mp.OPERATION_TYPES or target not in ("legacy", "durable"):
        raise HTTPException(422, "operation_type_mode_invalid")
    with session_factory() as db:
        try:
            if db.get_bind().dialect.name == "sqlite":
                db.execute(text("BEGIN IMMEDIATE"))
            row = db.execute(select(mp.ProvisioningTypeMode).where(
                mp.ProvisioningTypeMode.operation_type == operation_type).with_for_update()).scalar_one()
            if row.mode == target:
                db.rollback()
                return dict(operation_type=operation_type, mode=target)
            if target == "durable":
                if operation_type not in DURABLE_WIRED:
                    raise HTTPException(409, dict(code="durable_callers_not_wired", operation_type=operation_type))
                panel = db.get(models.PanelSettings, 1)
                if panel is not None and panel.ha_enabled:
                    raise HTTPException(409, "provisioning_ha_unsupported")
                runtime = db.get(mp.ProvisioningRuntimeState, 1)
                if runtime.gate_mode != "enforced":
                    raise HTTPException(409, "gate_not_enforced")
                identity = _identity()
                if runtime.owner_state != "active" or (runtime.owner_host_id, runtime.owner_boot_id) != (
                        identity.host_id, identity.boot_id):
                    raise HTTPException(409, "ownership_not_active")
                nodes = readiness.node_contract_blockers(db)
                if nodes:
                    raise HTTPException(409, dict(code="node_contract_not_ready", items=nodes))
                if operation_type == "purchase":
                    # L6: claims for the purchases made before, in this same
                    # transaction as the switch (Lifecycle 5.5, 16).
                    from . import provisioning_claims
                    provisioning_claims.backfill_existing_purchases(db, admin_id)
            else:
                from . import wallet_service
                if wallet_service.lock_phase_for_change(db) != "normal":
                    # Receipt Void 21: after the wallet cutover every purchase
                    # path is durable; the legacy writer no longer exists.
                    raise HTTPException(409, "wallet_enforced_requires_durable")
                pending = db.execute(select(mp.ProvisioningOperation.id).where(
                    mp.ProvisioningOperation.operation_type == operation_type,
                    mp.ProvisioningOperation.state.notin_(TERMINAL)).order_by(mp.ProvisioningOperation.id)).scalars().all()
                if pending:
                    raise HTTPException(409, dict(code="provisioning_operations_pending", operation_ids=pending))
            db.execute(mp.ProvisioningTypeMode.__table__.update().where(
                mp.ProvisioningTypeMode.operation_type == operation_type,
                mp.ProvisioningTypeMode.version == row.version).values(
                mode=target, changed_at=dt.datetime.utcnow(), version=mp.ProvisioningTypeMode.version + 1))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return dict(operation_type=operation_type, mode=target)


def reset_gate_stats(session_factory, admin_id):
    """Zero the two blocker counters (10.6). Only after the cause is fixed and
    every backend process restarted; contention telemetry is kept."""
    provisioning_schema.assert_ready()
    with session_factory() as db:
        db.execute(mp.ProvisioningGateStat.__table__.update().values(
            writer_not_instrumented_count=0, mode_lock_miss_count=0, updated_at=dt.datetime.utcnow()))
        db.commit()
    node_gate.counters.reset()
    return dict(reset=True)


# ------------------------------------------------------------------ node contract verify (5.10)
def verify_node_contract(session_factory, admin_id, node_id):
    """POST /nodes/{id}/contract/verify: a live probe, only in 'enforced' and
    only from the runner. A disabled node may be verified (then enabled)."""
    from . import legacy_remote, provisioning_contracts
    from .transport_guard import GateViolation
    provisioning_schema.assert_ready()
    with session_factory() as db:
        node = db.get(models.Node, node_id)
        if node is None:
            raise HTTPException(404, "node_not_found")
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        if runtime.gate_mode != "enforced":
            raise HTTPException(409, "gate_not_enforced")
        db.expunge(node)
    backend = legacy_remote.backend_of(node)
    try:
        observed = legacy_remote.dispatch_probe(node)
    except GateViolation as exc:
        raise HTTPException(409, str(exc) or "ownership_lock_host_mismatch") from None
    response = {"node_id": node_id, "backend": backend, "ready": False, "reason": observed.get("reason"),
                "cleanup": observed.get("cleanup"), "probe_identity": observed.get("probe_identity")}
    if observed.get("ready"):
        with session_factory() as db:
            try:
                fresh = db.get(models.Node, node_id)
                provisioning_contracts.record_verified(
                    db, fresh, backend, contract=observed["contract"],
                    server_fingerprint=observed["server_fingerprint"],
                    endpoint_at_dispatch=observed["endpoint_fingerprint"], actor_admin_id=admin_id)
                db.commit()
                response.update(ready=True, reason=None)
            except Exception:
                db.rollback()
                raise
    return response
