"""Periodic WireGuard interface-address reconciliation (Lifecycle 10.5).

Once at startup and every RECONCILE_MINUTES: for every enabled MikroTik node
with a WireGuard interface, apply the 9.1 interface-address contract (add or
widen toward Node.mt_client_subnet, never narrow) through the gated legacy
path - inline under writer_gate in off/shadow, in the runner in 'enforced' -
and upsert provisioning_node_reconciliations. It runs only while at least one
operation type is durable, exactly as the design says; it never touches a
peer. It finds drift made outside this system; it is not what makes
concurrency safe (the node gate is).
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select

from .. import models, models_provisioning as mp
from . import legacy_remote, provisioning_schema
from .mikrotik_client import MikrotikClient

logger = logging.getLogger(__name__)
RECONCILE_MINUTES = 10                 # Lifecycle 18 decision 13 (default)


def _due(db) -> bool:
    return db.execute(select(mp.ProvisioningTypeMode.operation_type).where(
        mp.ProvisioningTypeMode.mode == "durable")).first() is not None


def reconcile_node(session_factory, node_id: int) -> dict:
    with session_factory() as db:
        node = db.get(models.Node, node_id)
        if node is None or not node.enabled or node.type != models.NodeType.mikrotik or not node.mt_wireguard_interface:
            return {"node_id": node_id, "result": "skipped"}
        subnet = node.mt_client_subnet or "10.66.66.0/24"
        db.expunge(node)
    try:
        outcome = legacy_remote.run(node, "wg_reconcile", factory=MikrotikClient.for_node,
                                    interface=node.mt_wireguard_interface, desired_subnet=subnet)
        result, router_address, db_subnet = outcome["result"], outcome["router_address"], outcome["db_subnet"]
    except Exception as exc:  # noqa: BLE001 - an unreachable node is a recorded result, not a crash
        logger.warning("wireguard reconciliation: node %s unreachable (%s)", node_id, type(exc).__name__)
        result, router_address, db_subnet = "unreachable", None, subnet
    with session_factory() as db:
        row = db.get(mp.ProvisioningNodeReconciliation, node_id)
        now = dt.datetime.utcnow()
        if row is None:
            row = mp.ProvisioningNodeReconciliation(node_id=node_id, last_run_at=now, result=result,
                                                    db_subnet=db_subnet, router_address=router_address,
                                                    consecutive_failures=int(result == "unreachable"))
            db.add(row)
        else:
            row.last_run_at, row.result, row.db_subnet, row.router_address = now, result, db_subnet, router_address
            row.consecutive_failures = (row.consecutive_failures or 0) + 1 if result == "unreachable" else 0
            row.version = (row.version or 0) + 1
        db.commit()
    if result in ("router_wider", "conflict"):
        logger.warning("wireguard reconciliation: node %s %s (db %s, router %s)", node_id, result, db_subnet,
                       router_address)
    return {"node_id": node_id, "result": result}


def run_once(session_factory=None) -> list:
    if session_factory is None:
        from ..database import SessionLocal as session_factory  # noqa: N813
    if not provisioning_schema.is_ready():
        return []
    with session_factory() as db:
        if not _due(db):
            return []
        node_ids = db.execute(select(models.Node.id).where(
            models.Node.enabled.is_(True), models.Node.type == models.NodeType.mikrotik,
            models.Node.mt_wireguard_interface.isnot(None)).order_by(models.Node.id)).scalars().all()
    results = []
    for node_id in node_ids:
        try:
            results.append(reconcile_node(session_factory, node_id))
        except Exception:  # noqa: BLE001 - one node never stops the others
            logger.exception("wireguard reconciliation failed for node %s", node_id)
    return results
