"""DB-only final Connection records from a persisted provisioning step.

Caller owns canonical leases/runtime/contract validation and the complete
T_final transaction. No commit, remote call, live caller or mode activation.
Approval operations remain refused by the transition layer until integration.
"""
from fastapi import HTTPException
from sqlalchemy import select

from .. import models
from . import provisioning_transitions as transitions
from .provisioning_identity import PPP, XRAY_MODES


def _locked(db, model, ident):
    return db.execute(select(model).where(model.id == ident).with_for_update()
                      .execution_options(populate_existing=True)).scalar_one_or_none()


def build_connection_core(db, step_id, version, *, user_id, purchase_id=None, purchase_batch=None):
    operation, step = transitions._step(db, step_id, version)
    expected = "staged" if step.backend == "radius_ppp" else "remote_created"
    if operation.operation_type not in ("create_user", "purchase", "add_connection") or (
            operation.state != "remote_complete" or step.state != expected or step.direction != "create"):
        raise HTTPException(409, "provisioning_step_transition_invalid")
    user = _locked(db, models.User, user_id)
    if user is None:
        raise HTTPException(409, "target_user_missing")
    if operation.target_user_id is not None:
        if user.id != operation.target_user_id:
            raise HTTPException(409, "provisioning_connection_mismatch")
    elif operation.operation_type != "create_user" or not operation.username_claim or user.username != operation.username_claim:
        raise HTTPException(409, "provisioning_connection_mismatch")
    purchase = _locked(db, models.Purchase, purchase_id) if purchase_id is not None else None
    if purchase_id is not None and (purchase is None or purchase.user_id != user_id):
        raise HTTPException(409, "provisioning_connection_mismatch")
    if purchase_batch is not None and (not isinstance(purchase_batch, str) or not 1 <= len(purchase_batch) <= 40):
        raise HTTPException(422, "provisioning_batch_invalid")
    node = _locked(db, models.Node, step.node_id)
    if node is None or not node.enabled:
        raise HTTPException(409, "node_unavailable")
    if step.backend == "mikrotik_wg":
        valid = step.protocol == "wireguard" and node.type == models.NodeType.mikrotik
        required = (step.wg_interface, step.wg_peer_name, step.wg_public_key,
                    step.wg_client_address, step.staged_wg_private_key)
        if step.wg_interface != node.mt_wireguard_interface:
            raise HTTPException(409, "provisioning_node_config_changed")
        fields = dict(wg_peer_name=step.wg_peer_name, wg_public_key=step.wg_public_key,
                      wg_client_address=step.wg_client_address, wg_private_key=step.staged_wg_private_key)
    elif step.backend == "radius_ppp" or step.backend == "softether":
        valid = ((step.protocol in PPP and node.type == models.NodeType.mikrotik) if step.backend == "radius_ppp"
                 else step.protocol == "softether" and node.type == models.NodeType.softether)
        required = (step.account_username, step.staged_password)
        fields = dict(ppp_username=step.account_username, ppp_password=step.staged_password)
    elif step.backend in XRAY_MODES:
        valid = step.protocol == "xray" and node.type == models.NodeType.xray
        if (node.xr_panel_mode or "ssh") != XRAY_MODES[step.backend] or (
                node.xr_inbound_tag, node.xr_panel_inbound_id) != (
                step.xr_inbound_tag, step.xr_panel_inbound_id):
            raise HTTPException(409, "provisioning_node_config_changed")
        required = (step.xr_email, step.staged_xr_uuid)
        fields = dict(xr_email=step.xr_email, xr_uuid=step.staged_xr_uuid, xr_flow=step.flow)
    else:
        raise HTTPException(409, "provisioning_backend_invalid")
    if not valid or not all(required):
        raise HTTPException(409, "provisioning_identity_incomplete")
    connection = models.Connection(user_id=user_id, node_id=node.id, type=models.ConnectionType(step.protocol),
        purchase_id=purchase_id, purchase_batch=purchase_batch,
        package_name_snapshot=purchase.package_name_snapshot if purchase else None,
        speed_limit_mbps=step.speed_limit_mbps, max_concurrent_sessions=step.max_concurrent_sessions,
        enabled=True, **fields)
    db.add(connection)
    db.flush()
    transitions.activate(db, step.id, version, connection.id)
    return connection
