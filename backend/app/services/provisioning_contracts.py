"""Node contract guards (5.10): readiness checks, automatic invalidation of a
stale 'ready' row, and the single writer of a verified row (after a probe)."""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select

from .. import models, models_provisioning as mp
from .adapter_base import InvalidMatcher
from . import provisioning_contract_rules as contract_rules
from .provisioning_identity import PPP, XRAY_MODES

adapter_version = contract_rules.adapter_version
validate_contract = contract_rules.validate_contract
_FIELDS = contract_rules.ENDPOINT_FIELDS

def config_fingerprint(node):
    values = {name: getattr(node, name, None) for name in _FIELDS}
    values["type"] = node.type.value
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def backend_for(node, protocol):
    if node.type == models.NodeType.mikrotik:
        if protocol == "wireguard":
            return "mikrotik_wg"
        if protocol in PPP:
            return "radius_ppp"
    if node.type == models.NodeType.softether and protocol == "softether":
        return "softether"
    if node.type == models.NodeType.xray and protocol == "xray":
        for backend, mode in XRAY_MODES.items():
            if (node.xr_panel_mode or "ssh") == mode:
                return backend
    raise HTTPException(409, "provisioning_backend_invalid")


def require_ready(db, node, backend):
    if backend == "radius_ppp":
        return None
    row = db.execute(select(mp.ProvisioningNodeContract).where(
        mp.ProvisioningNodeContract.node_id == node.id, mp.ProvisioningNodeContract.backend == backend)
        .with_for_update(read=True).execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None or row.state != "ready" or row.adapter_version != adapter_version(backend) or (
            row.config_fingerprint != config_fingerprint(node)):
        raise HTTPException(409, "node_contract_not_ready")
    try:
        contract = validate_contract(row.contract, backend)
    except (ValueError, TypeError, InvalidMatcher):
        raise HTTPException(409, "node_contract_not_ready") from None
    return contract


def invalidate_stale(db) -> list:
    """5.10 automatic invalidation: a 'ready' row whose adapter version, node
    configuration or backend no longer matches becomes 'invalidated' (small
    CAS UPDATE each). Caller commits. Returns [(node_id, backend, reason)]."""
    import datetime as dt
    from sqlalchemy import update
    changed = []
    rows = db.execute(select(mp.ProvisioningNodeContract).where(mp.ProvisioningNodeContract.state == "ready")
                      .order_by(mp.ProvisioningNodeContract.node_id)).scalars().all()
    for row in rows:
        node = db.get(models.Node, row.node_id)
        reason = None
        if node is None:
            reason = "backend_changed"
        else:
            try:
                live_backend = backend_for(node, "wireguard" if node.type == models.NodeType.mikrotik else
                                           "softether" if node.type == models.NodeType.softether else "xray")
            except Exception:  # noqa: BLE001
                live_backend = None
            if live_backend != row.backend:
                reason = "backend_changed"
            elif row.adapter_version != adapter_version(row.backend):
                reason = "adapter_version_changed"
            elif row.config_fingerprint != config_fingerprint(node):
                reason = "config_changed"
        if reason is None:
            continue
        table = mp.ProvisioningNodeContract.__table__
        result = db.execute(update(table).where(table.c.node_id == row.node_id, table.c.backend == row.backend,
                                                table.c.version == row.version, table.c.state == "ready").values(
            state="invalidated", invalidated_at=dt.datetime.utcnow(), invalidation_reason=reason,
            version=table.c.version + 1))
        if result.rowcount == 1:
            changed.append((row.node_id, row.backend, reason))
    return changed


def record_verified(db, node, backend, *, contract: dict, server_fingerprint: str, endpoint_at_dispatch: str,
                    actor_admin_id: int) -> None:
    """Write the 'ready' row of a successful probe. Caller commits. Refuses if
    the node's configuration changed since the probe was dispatched."""
    import datetime as dt
    from .provisioning_contract_rules import endpoint_fingerprint
    from . import legacy_remote
    live = endpoint_fingerprint({"n_" + k: v for k, v in legacy_remote.endpoint(node).items()})
    if live != endpoint_at_dispatch:
        raise HTTPException(409, "node_config_changed_during_probe")
    raw = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    validate_contract(raw, backend)
    now = dt.datetime.utcnow()
    row = db.execute(select(mp.ProvisioningNodeContract).where(
        mp.ProvisioningNodeContract.node_id == node.id, mp.ProvisioningNodeContract.backend == backend)
        .with_for_update()).scalar_one_or_none()
    values = dict(state="ready", adapter_version=adapter_version(backend), server_fingerprint=server_fingerprint,
                  config_fingerprint=config_fingerprint(node), contract=raw, verified_by_admin_id=actor_admin_id,
                  verified_at=now, invalidated_at=None, invalidation_reason=None)
    if row is None:
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend=backend, **values))
    else:
        for key, value in values.items():
            setattr(row, key, value)
        row.version = (row.version or 0) + 1
    db.flush()
