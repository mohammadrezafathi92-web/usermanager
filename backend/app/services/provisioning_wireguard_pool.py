"""Fenced, DB-only WireGuard T1 allocation (Lifecycle 9.1).

Caller owns runtime/host/permission validation, acquisition of canonical leases,
begin_business(), rollback on any error and one commit. No live caller, remote
I/O, key generation or activation. Legacy user_ops allocation is unchanged.
"""
import ipaddress
import re

from fastapi import HTTPException
from sqlalchemy import or_, select

from .. import models, models_provisioning as mp, models_receipt_void as rv
from . import provisioning_transitions as transitions, receipt_void_schema, resource_leases, wallet_service


def _addresses(value):
    if not value:
        return []
    try:
        addresses = [ipaddress.ip_interface(part.strip()).ip for part in value.split(",")]
        if any(address.version != 4 for address in addresses):
            raise ValueError()
        return addresses
    except (ValueError, AttributeError):
        raise HTTPException(409, "wg_pool_address_invalid") from None


def _free_run(subnet, used, count):
    run = []
    for address in subnet.hosts():
        if address == subnet.network_address + 1 or address in used:
            run = []
        else:
            run.append(address)
            if len(run) == count:
                return run
    return None


def allocate(db, step_id, version, leases, count=1):
    """Write the step's IP claim and optional subnet expansion together.

    A retry with the current step version and same count returns its original
    claim, never a second allocation. Removed/verified claims are reusable;
    abandoned remote resources retain their IP claim even after compensation.
    """
    if type(count) is not int or not 1 <= count <= 65533:
        raise HTTPException(422, "wg_pool_count_invalid")
    receipt_void_schema.assert_ready()
    wallet_service.require_legacy_phase(db)
    epoch = db.execute(select(rv.wallet_runtime_state.c.epoch).where(
        rv.wallet_runtime_state.c.id == 1).with_for_update(read=True)).scalar_one()
    tokens = tuple(leases)
    resource_leases.revalidate(db, tokens)
    operation, step = transitions._step(db, step_id, version)
    if operation.wallet_epoch_at_start != epoch:
        raise HTTPException(409, "wallet_epoch_changed")
    required = {f"provisioning_op:{operation.id}", f"node:{step.node_id}:wg_pool"}
    if not required.issubset({token.resource_key for token in tokens}) or len({token.owner for token in tokens}) != 1 or any(
            not re.fullmatch(rf"op:{operation.id}:[1-9][0-9]*", token.owner) for token in tokens):
        raise HTTPException(409, "provisioning_lease_scope_invalid")
    if operation.operation_type not in ("create_user", "add_connection", "purchase") or operation.state != "prepared" or (
            step.direction != "create" or step.state != "staged" or step.remote_attempted or
            step.backend != "mikrotik_wg" or step.protocol != "wireguard"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    node = db.execute(select(models.Node).where(models.Node.id == step.node_id).with_for_update()
        .execution_options(populate_existing=True)).scalar_one_or_none()
    if node is None or not node.enabled or node.type != models.NodeType.mikrotik or (
            not step.wg_interface or step.wg_interface != node.mt_wireguard_interface):
        raise HTTPException(409, "wg_pool_node_changed")
    try:
        subnet = ipaddress.ip_network(node.mt_client_subnet or "10.66.66.0/24")
        if subnet.version != 4 or subnet.prefixlen > 30:
            raise ValueError()
    except ValueError:
        raise HTTPException(409, "wg_pool_subnet_invalid") from None
    existing = _addresses(step.wg_client_address)
    if existing:
        if len(existing) != count or len(set(existing)) != count or any(
                int(existing[index]) != int(existing[index - 1]) + 1 for index in range(1, count)):
            raise HTTPException(409, "wg_pool_claim_changed")
        if any(address not in subnet or address in (subnet.network_address, subnet.broadcast_address,
                subnet.network_address + 1) for address in existing):
            raise HTTPException(409, "wg_ip_outside_subnet")
        return step
    used = set()
    for value in db.execute(select(models.Connection.wg_client_address).where(
            models.Connection.node_id == node.id, models.Connection.type == models.ConnectionType.wireguard)).scalars():
        used.update(_addresses(value))
    staged = db.execute(select(mp.ProvisioningStep.wg_client_address).join(mp.ProvisioningOperation,
        mp.ProvisioningOperation.id == mp.ProvisioningStep.operation_id).where(
        mp.ProvisioningStep.node_id == node.id, mp.ProvisioningStep.protocol == "wireguard",
        or_(mp.ProvisioningStep.remote_outcome == "abandoned",
            (mp.ProvisioningOperation.state.notin_(transitions.TERMINAL) &
             mp.ProvisioningStep.state.notin_(("active", "removed")))))).scalars()
    for value in staged:
        used.update(_addresses(value))
    original = subnet
    run = _free_run(subnet, used, count)
    while run is None:
        if subnet.prefixlen <= 16:
            raise HTTPException(409, "wg_pool_exhausted")
        # Keep old gateway addresses unavailable while widening the network.
        used.add(subnet.network_address + 1)
        subnet = subnet.supernet(prefixlen_diff=1)
        run = _free_run(subnet, used, count)
    address_text = ",".join(f"{address}/32" for address in run)
    if len(address_text) > 64:
        raise HTTPException(422, "wg_pool_claim_too_long")
    if subnet != original:
        node.mt_client_subnet = str(subnet)
    result = transitions._write_step(db, step, "staged", wg_client_address=address_text,
        wg_gateway_with_prefix=f"{subnet.network_address + 1}/{subnet.prefixlen}",
        wg_subnet_expanded=subnet != original)
    db.flush()
    return result
