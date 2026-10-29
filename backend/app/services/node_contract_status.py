"""Save-triggered verification, outside the versioned contract implementation.

The save lock is NOT the runner's node lock: the child must acquire that
itself. It serializes HTTP saves and both probe entry points across workers.
The DB row lock and private attempt token fence completion. No network on GET.
"""
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import secrets

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .. import models, models_provisioning as mp
from . import gate_locks, legacy_remote, provisioning_contracts as contracts, provisioning_schema
from .transport_guard import GateViolation

# Fixed public messages only; never expose exceptions or remote response bodies.
REASONS = frozenset({
    "not_checked", "config_changed", "adapter_version_changed", "backend_changed",
    "gate_not_enforced", "schema_not_ready", "backend_unsupported", "verification_failed",
    "node_config_changed_during_probe", "probe_cleanup_unknown", "verification_interrupted",
    "ownership_or_lock_unavailable", "wireguard_interface_missing", "wireguard_interface_create_failed",
    "wireguard_interface_not_ready", "server_version_unreadable",
    "read_before_not_absent", "read_before_not_a_complete_answer", "create_failed",
    "read_after_create_not_match", "delete_unverified", "read_after_delete_not_absent",
    "read_not_exist_unstable", "delete_not_exist_unstable", "read_failed", "probe_cleanup_verified_absent",
})


def verification_fields():
    return set(contracts._FIELDS).union(*(set(group) for group in legacy_remote.SECRETS.values()),
                                        *(set(group) for group in legacy_remote.EXTRA_PUBLIC.values()))


def fingerprint(node):
    # Secret hash is private DB bookkeeping, never serialized to the API.
    fields = verification_fields()
    values = {name: getattr(node, name, None) for name in sorted(fields)}
    values["type"] = node.type.value
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextlib.contextmanager
def save_lock(db, node_id):
    namespace = hashlib.sha256(str(db.get_bind().url.render_as_string(hide_password=True)).encode()).hexdigest()[:20]
    lock = gate_locks.FileLock(os.path.join(gate_locks.lock_base_dir(), "node-save-" + namespace, str(node_id) + ".lock"))
    try:
        lock.acquire(shared=False, timeout=0)
    except (gate_locks.LockTimeout, gate_locks.LockUnavailable):
        raise HTTPException(409, "node_verification_busy") from None
    try:
        yield
    finally:
        lock.release()


def _stored(node):
    try:
        value = json.loads(node.contract_check_json or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def refresh_locked(db, node_id):
    # Scope/auth lookup may have opened a MariaDB REPEATABLE READ snapshot.
    # End that read transaction only after obtaining the save lock, then use
    # a current locking read. Never trust an ORM identity loaded before it.
    db.rollback()
    node = db.execute(select(models.Node).where(models.Node.id == node_id).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()
    if node is None:
        raise HTTPException(404, "node_not_found")
    return node


def describe(db, node):
    stored = _stored(node)
    reason = stored.get("reason")
    reason = reason if reason in REASONS else "verification_failed" if reason else None
    result = dict(state="not_checked", ready=False, backend=None, reason="not_checked",
                  cleanup=stored.get("cleanup") if stored.get("cleanup") in ("clean", "unknown") else None,
                  cleanup_inspection=stored.get("cleanup_inspection") if stored.get("cleanup_inspection") in ("absent", "present", "ambiguous") else None,
                  checked_at=stored.get("checked_at"), retry_allowed=True)
    try:
        backend = legacy_remote.backend_of(node)
        result["backend"] = backend
        if not provisioning_schema.is_ready():
            return dict(result, state="unavailable", reason="schema_not_ready", retry_allowed=False)
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        # Unknown cleanup takes precedence even after settings/mode changes.
        if stored.get("state") in ("cleanup_unknown", "verifying"):
            identity = stored.get("probe_identity")
            recovery = (stored.get("state") == "cleanup_unknown" and backend == "mikrotik_wg" and
                        isinstance(identity, str) and re.fullmatch(r"um-probe-[0-9a-f]{16}", identity) is not None and
                        node.contract_check_fingerprint == fingerprint(node))
            manual_identity_required = (stored.get("state") == "cleanup_unknown" and identity is None and
                                        backend == "mikrotik_wg" and
                                        node.contract_check_fingerprint == fingerprint(node))
            return dict(result, state="cleanup_unknown",
                        reason="probe_cleanup_unknown" if stored.get("state") == "cleanup_unknown" else "verification_interrupted",
                        retry_allowed=False,
                        probe_identity=identity if recovery else None, cleanup_recovery_available=recovery,
                        cleanup_manual_identity_required=manual_identity_required)
        if runtime is None or runtime.gate_mode != "enforced":
            return dict(result, state="gate_not_enforced", reason="gate_not_enforced", retry_allowed=False)
        row = db.get(mp.ProvisioningNodeContract, (node.id, backend))
        if node.contract_check_fingerprint and node.contract_check_fingerprint != fingerprint(node):
            return dict(result, state="stale", reason="config_changed")
        try:
            contracts.require_ready(db, node, backend)
            return dict(result, state="ready", ready=True, reason=None)
        except HTTPException:
            pass
        if row is not None and stored.get("state") != "failed":
            reason = "adapter_version_changed" if row.adapter_version != contracts.adapter_version(backend) else "config_changed" if row.config_fingerprint != contracts.config_fingerprint(node) else row.invalidation_reason
        return dict(result, state="failed" if stored.get("state") == "failed" else "stale" if row is not None else "not_checked",
                    reason=reason if reason in REASONS else "verification_failed" if stored else "not_checked")
    except (StopIteration, ValueError):
        return dict(result, state="unsupported", reason="backend_unsupported", retry_allowed=False)


def attach(db, node):
    node.contract_status = describe(db, node)
    return node


def invalidate(db, node):
    """Credential edits also revoke old contracts, whose public hash omits secrets."""
    now = dt.datetime.utcnow()
    for row in db.execute(select(mp.ProvisioningNodeContract).where(mp.ProvisioningNodeContract.node_id == node.id).with_for_update()).scalars():
        if row.state == "ready":
            row.state, row.invalidation_reason, row.invalidated_at = "invalidated", "config_changed", now
            row.version = (row.version or 0) + 1


def _write(node, state, reason=None, cleanup=None, identity=None):
    value = dict(state=state, reason=reason if reason in REASONS else "verification_failed" if reason else None,
                 cleanup=cleanup, checked_at=dt.datetime.utcnow().isoformat() + "Z")
    # The only retained remote string has a tightly constrained namespaced form.
    if isinstance(identity, str) and re.fullmatch(r"um-probe-[0-9a-f]{16}(?:@usermanager\.local)?", identity):
        value["probe_identity"] = identity
    node.contract_check_json = json.dumps(value, separators=(",", ":"))


def verify_locked(db, node, actor_id):
    """Caller owns save_lock. Commit settings BEFORE remote I/O; never rollback a save."""
    if _stored(node).get("state") in ("cleanup_unknown", "verifying"):
        return attach(db, node)
    state = describe(db, node)
    if state["state"] in ("unavailable", "gate_not_enforced", "unsupported"):
        _write(node, state["state"], state["reason"])
        db.commit()
        return attach(db, node)
    backend = legacy_remote.backend_of(node)
    expected = fingerprint(node)
    token = secrets.token_hex(24)
    invalidate(db, node)
    node.contract_check_token = token
    node.contract_check_fingerprint = expected
    _write(node, "verifying", "verification_interrupted", "unknown")
    db.commit()
    db.refresh(node)
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    # Detached immutable snapshot avoids accidentally reading expired ORM fields.
    with factory() as snapshot_db:
        snapshot = snapshot_db.get(models.Node, node.id)
        if snapshot is None:
            raise HTTPException(404, "node_not_found")
        snapshot_db.expunge(snapshot)
    try:
        observed = legacy_remote.dispatch_probe(snapshot)
    except GateViolation:
        # The writer passage refused before runner dispatch; no temporary write.
        observed = dict(ready=False, reason="ownership_or_lock_unavailable", cleanup="clean")
    except Exception:
        # No exception string: a lost child response can hide a created resource.
        observed = dict(ready=False, reason="ownership_or_lock_unavailable", cleanup="unknown")
    db.rollback()  # discard any read snapshot before acquiring the fresh row lock
    fresh = db.execute(select(models.Node).where(models.Node.id == node.id).with_for_update().execution_options(populate_existing=True)).scalar_one_or_none()
    if fresh is None:
        raise HTTPException(404, "node_not_found")
    if fresh.contract_check_token != token:
        return attach(db, fresh)
    cleanup = "clean" if observed.get("cleanup") == "clean" else "unknown"
    if cleanup == "unknown":
        _write(fresh, "cleanup_unknown", "probe_cleanup_unknown", cleanup, observed.get("probe_identity"))
    elif fingerprint(fresh) != expected or legacy_remote.backend_of(fresh) != backend:
        _write(fresh, "failed", "node_config_changed_during_probe", cleanup)
    elif observed.get("ready") is True:
        try:
            contracts.record_verified(db, fresh, backend, contract=observed["contract"],
                server_fingerprint=observed["server_fingerprint"], endpoint_at_dispatch=observed["endpoint_fingerprint"], actor_admin_id=actor_id)
            _write(fresh, "ready", cleanup=cleanup)
        except (HTTPException, ValueError, TypeError, KeyError):
            _write(fresh, "failed", "verification_failed", cleanup)
    else:
        _write(fresh, "failed", observed.get("reason") or "verification_failed", cleanup)
    db.commit()
    db.refresh(fresh)
    return attach(db, fresh)


def verify_probe_cleanup_locked(db, node, actor_id, supplied_identity=None):
    """Read-only inspect exact stored MikroTik test peer; unlock only on absence."""
    stored = _stored(node)
    identity = stored.get("probe_identity")
    if identity is None:
        identity = supplied_identity
    elif supplied_identity is not None and supplied_identity != identity:
        raise HTTPException(409, "probe_cleanup_identity_mismatch")
    if (stored.get("state") != "cleanup_unknown" or legacy_remote.backend_of(node) != "mikrotik_wg" or
            not isinstance(identity, str) or not re.fullmatch(r"um-probe-[0-9a-f]{16}", identity) or
            node.contract_check_fingerprint != fingerprint(node)):
        raise HTTPException(409, "probe_cleanup_recovery_unavailable")
    try:
        result = legacy_remote.dispatch_probe_cleanup_check(node, identity)
    except GateViolation:
        raise HTTPException(409, "probe_cleanup_inspection_unavailable") from None
    except Exception:
        raise HTTPException(503, "probe_cleanup_inspection_unavailable") from None
    if result == "absent":
        _write(node, "failed", "probe_cleanup_verified_absent", "clean")
        value = _stored(node)
        value.update(cleanup_inspection="absent", cleanup_inspected_by=actor_id,
                     cleanup_inspected_identity=identity)
        node.contract_check_json = json.dumps(value, separators=(",", ":"))
    else:
        value = dict(stored)
        value.update(cleanup_inspection=result, cleanup_inspected_by=actor_id, probe_identity=identity,
                     checked_at=dt.datetime.utcnow().isoformat() + "Z")
        node.contract_check_json = json.dumps(value, separators=(",", ":"))
    db.commit()
    db.refresh(node)
    return attach(db, node)
