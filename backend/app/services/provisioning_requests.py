"""Trusted durable requests built from what a production caller already
validated (Lifecycle 7.5). Each builder only TRANSLATES: the caller has
authenticated and authorized the actor and the target; T1 re-validates the
targets under its own locks and leases.

business_key: "req:{tenant_scope_key}:{idempotency key}" (5.1); the same key
with the same request is the same operation, with another request a 409.
"""
from __future__ import annotations

from sqlalchemy.orm import sessionmaker

from .. import models
from . import provisioning_delete_preparation as delete_preparation
from . import provisioning_finalization as final, provisioning_preparation as preparation
from . import wallet_accounts


def session_factory_for(db):
    """Short sessions on the SAME database as the caller's request."""
    return sessionmaker(bind=db.get_bind(), autoflush=False, autocommit=False)


def user_snapshot(user: models.User) -> final.UserSnapshot:
    return final.UserSnapshot(username=user.username, telegram_id=user.telegram_id,
                              owner_admin_id=user.owner_admin_id)


def protocol_value(protocol) -> str:
    return protocol.value if hasattr(protocol, "value") else str(protocol)


def add_connection(db, user: models.User, node: models.Node, protocol, *, flow: str = "",
                   max_concurrent_sessions: int | None = 1, speed_limit_mbps: int | None = None,
                   purchase_batch: str | None = None, purchase_id: int | None = None,
                   key: str) -> preparation.Preparation:
    tenant = wallet_accounts.tenant_scope(db, user.owner_admin_id)
    return preparation.Preparation(
        operation_type="add_connection", business_key=f"req:{tenant}:{key}", tenant_scope_key=tenant,
        target_user_id=user.id,
        intent=final.FinalIntent(user=user_snapshot(user), purchase_batch=purchase_batch, purchase_id=purchase_id),
        slots=[preparation.Slot(slot_key="request_slot:0", node_id=node.id, protocol=protocol_value(protocol),
                                flow=flow or "", max_concurrent_sessions=max_concurrent_sessions or 1,
                                speed_limit_mbps=speed_limit_mbps)])


def delete(db, resource_kind: str, resource_id: int, owner_admin_id: int | None, *, key: str):
    tenant = wallet_accounts.tenant_scope(db, owner_admin_id)
    return delete_preparation.DeleteRequest(resource_kind=resource_kind, resource_id=resource_id,
                                            business_key=f"delete:{resource_kind}:{resource_id}:{key}",
                                            tenant_scope_key=tenant)


def created_connections(db, operation_id: int) -> list:
    """The Connection rows a completed create operation built, in slot order."""
    from sqlalchemy import select
    from .. import models_provisioning as mp
    ids = db.execute(select(mp.ProvisioningStep.connection_id).where(
        mp.ProvisioningStep.operation_id == operation_id, mp.ProvisioningStep.direction == "create")
        .order_by(mp.ProvisioningStep.step_order)).scalars().all()
    return [db.get(models.Connection, ident) for ident in ids if ident is not None]


def run_delete(db, resource_kind: str, resource_id: int, owner_admin_id, *, key: str, actor_kind: str,
               actor_id=None):
    """delete_connection / delete_purchase / delete_user through the durable
    machine (Lifecycle 12): T1 disables the scope, each connection is removed
    by identity, T_final deletes the rows only when every one is gone."""
    from . import provisioning_durable
    request = delete(db, resource_kind, resource_id, owner_admin_id, key=key)
    factory = session_factory_for(db)
    db.rollback()
    return provisioning_durable.run(factory, request, actor_kind=actor_kind, actor_id=actor_id)
