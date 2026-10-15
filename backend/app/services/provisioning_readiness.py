"""p6_ready(): the read-only definition of "P6 complete" (Lifecycle 7.7).

Receipt Void's joint activation (P9a) waits for P9, and P9 waits for a
complete P6; Lifecycle 7.7 defines that completeness as this predicate. It
only READS: no lock is taken, no row is written, no mode is changed, and no
node is contacted. Every condition that does not hold is returned as a
blocker with the exact items, so the operator can see what to close; the
answer is True only when the list is empty. There is no override.

Conditions, in the order of 7.7:
  1. every operation_type is 'durable';
  2. gate_mode = 'enforced', owner_state = 'active', and the owner is THIS
     host and boot (10.4, 10.6);
  3. every enabled node has a 'ready' node contract for its backend with the
     current adapter version and configuration fingerprint (5.10);
  4. no non-terminal operation of an approval registered under 'shadow';
  5. no 'mutating' approval registered under 'shadow' whose operation is
     'completed';
  6. no non-terminal approval_recovery for an approval registered under
     'shadow'.
"""
from __future__ import annotations

from sqlalchemy import select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import provisioning_contracts as contracts, provisioning_schema, receipt_void_schema
from .provisioning_host import HostIdentityUnavailable, read_identity
from .provisioning_transitions import TERMINAL

RECOVERY_OPEN = ("pending", "running", "cleanup_required")

# Code that P6 needs and that does not exist yet. Each entry is removed by
# the change that implements it; while any remains, status never reports
# p6_ready, whatever the data says.
IMPLEMENTATION_GAPS: tuple = ()


def _node_backend(node):
    """The one backend whose contract an enabled node needs, or None (PPP-only)."""
    if node.type == models.NodeType.mikrotik:
        return "mikrotik_wg"
    if node.type == models.NodeType.softether:
        return "softether"
    if node.type == models.NodeType.xray:
        return contracts.backend_for(node, "xray")
    return None


def node_contract_blockers(db) -> list[dict]:
    """Enabled nodes without a current 'ready' contract (5.10), by ID."""
    version = contracts.adapter_version()
    found = []
    for node in db.execute(select(models.Node).where(models.Node.enabled.is_(True)).order_by(models.Node.id)).scalars():
        try:
            backend = _node_backend(node)
        except Exception:  # noqa: BLE001 - an unknown/invalid backend is not ready, never skipped
            found.append({"node_id": node.id, "backend": None, "reason": "backend_invalid"})
            continue
        if backend is None:
            continue
        row = db.execute(select(mp.ProvisioningNodeContract).where(
            mp.ProvisioningNodeContract.node_id == node.id,
            mp.ProvisioningNodeContract.backend == backend)).scalar_one_or_none()
        if row is None or row.state != "ready":
            reason = "unverified" if row is None or row.state == "unverified" else "invalidated"
        elif row.adapter_version != version:
            reason = "adapter_version_changed"
        elif row.config_fingerprint != contracts.config_fingerprint(node):
            reason = "config_changed"
        else:
            try:
                contracts.validate_contract(row.contract, backend)
                continue
            except Exception:  # noqa: BLE001 - an unreadable contract is not ready
                reason = "contract_invalid"
        found.append({"node_id": node.id, "backend": backend, "reason": reason})
    return found


def p6_ready(db, identity=None) -> tuple[bool, list[dict]]:
    """(ready, blockers). Read-only; `identity` defaults to this host's."""
    blockers: list[dict] = []
    if not provisioning_schema.is_ready():
        return False, [{"code": "provisioning_schema_not_ready"}]
    if not receipt_void_schema.is_ready():
        return False, [{"code": "receipt_void_schema_not_ready"}]

    modes = dict(db.execute(select(mp.ProvisioningTypeMode.operation_type, mp.ProvisioningTypeMode.mode)).all())
    legacy = sorted(kind for kind in mp.OPERATION_TYPES if modes.get(kind) != "durable")
    if legacy:
        blockers.append({"code": "operation_types_not_durable", "items": legacy})

    runtime = db.execute(select(mp.ProvisioningRuntimeState).where(mp.ProvisioningRuntimeState.id == 1)).scalar_one_or_none()
    if runtime is None:
        blockers.append({"code": "provisioning_runtime_missing"})
    else:
        if runtime.gate_mode != "enforced":
            blockers.append({"code": "gate_not_enforced", "gate_mode": runtime.gate_mode})
        if runtime.owner_state != "active":
            blockers.append({"code": "ownership_not_active", "owner_state": runtime.owner_state})
        if identity is None:
            try:
                identity = read_identity()
            except HostIdentityUnavailable:
                identity = None
        if identity is None:
            blockers.append({"code": "host_identity_unavailable"})
        elif (runtime.owner_host_id, runtime.owner_boot_id) != (identity.host_id, identity.boot_id):
            blockers.append({"code": "owner_not_this_host"})

    nodes = node_contract_blockers(db)
    if nodes:
        blockers.append({"code": "node_contract_not_ready", "items": nodes})

    approvals = rv.receipt_approvals
    operations = mp.ProvisioningOperation
    shadow_open = db.execute(select(operations.id, operations.approval_uuid).join(
        approvals, approvals.c.approval_uuid == operations.approval_uuid).where(
        approvals.c.registered_under_mode == "shadow", operations.state.notin_(TERMINAL))
        .order_by(operations.id)).all()
    if shadow_open:
        blockers.append({"code": "shadow_operation_open",
                         "items": [{"operation_id": row.id, "approval_uuid": row.approval_uuid} for row in shadow_open]})
    shadow_mutating = db.execute(select(approvals.c.approval_uuid).join(
        operations, operations.approval_uuid == approvals.c.approval_uuid).where(
        approvals.c.registered_under_mode == "shadow", approvals.c.state == "mutating",
        operations.state == "completed").order_by(approvals.c.approval_uuid)).scalars().all()
    if shadow_mutating:
        blockers.append({"code": "shadow_approval_mutating", "items": sorted(set(shadow_mutating))})
    recoveries = rv.wallet_operations
    shadow_recovery = db.execute(select(recoveries.c.id, recoveries.c.business_key).join(
        approvals, approvals.c.approval_uuid == recoveries.c.business_key).where(
        recoveries.c.operation_type == "approval_recovery", recoveries.c.state.in_(RECOVERY_OPEN),
        approvals.c.registered_under_mode == "shadow").order_by(recoveries.c.id)).all()
    if shadow_recovery:
        blockers.append({"code": "shadow_approval_recovery_open",
                         "items": [{"wallet_operation_id": row.id, "approval_uuid": row.business_key}
                                   for row in shadow_recovery]})
    return not blockers, blockers
