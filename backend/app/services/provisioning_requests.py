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


def batch_of(business_key: str) -> str:
    """The purchase_batch of an operation's connections, derived from its
    business key: a retry of the same request is the same request (its hash
    matches), so it finds the same operation instead of a 409 or a second one."""
    import hashlib
    return hashlib.sha256(("purchase_batch|" + business_key).encode()).hexdigest()[:32]


def existing_operation(db, operation_type: str, business_key: str):
    from sqlalchemy import select
    from .. import models_provisioning as mp
    return db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.operation_type == operation_type,
        mp.ProvisioningOperation.business_key == business_key)).scalar_one_or_none()


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


def delete(db, resource_kind: str, resource_id: int, owner_admin_id: int | None, *, key: str,
           target_username: str | None = None):
    tenant = wallet_accounts.tenant_scope(db, owner_admin_id)
    return delete_preparation.DeleteRequest(resource_kind=resource_kind, resource_id=resource_id,
                                            business_key=f"delete:{resource_kind}:{resource_id}:{key}",
                                            tenant_scope_key=tenant, target_username=target_username)


def created_connections(db, operation_id: int) -> list:
    """The Connection rows a completed create operation built, in slot order."""
    from sqlalchemy import select
    from .. import models_provisioning as mp
    ids = db.execute(select(mp.ProvisioningStep.connection_id).where(
        mp.ProvisioningStep.operation_id == operation_id, mp.ProvisioningStep.direction == "create")
        .order_by(mp.ProvisioningStep.step_order)).scalars().all()
    return [db.get(models.Connection, ident) for ident in ids if ident is not None]


def run_delete(db, resource_kind: str, resource_id: int, owner_admin_id, *, key: str, actor_kind: str,
               actor_id=None, target_username: str | None = None):
    """delete_connection / delete_purchase / delete_user through the durable
    machine (Lifecycle 12): T1 disables the scope, each connection is removed
    by identity, T_final deletes the rows only when every one is gone."""
    from . import provisioning_durable
    request = delete(db, resource_kind, resource_id, owner_admin_id, key=key, target_username=target_username)
    factory = session_factory_for(db)
    db.rollback()
    return provisioning_durable.run(factory, request, actor_kind=actor_kind, actor_id=actor_id)


GB = 1024 ** 3


def package_snapshot(package) -> final.PackageSnapshot | None:
    if package is None:
        return None
    return final.PackageSnapshot(id=package.id, name=package.name, quota_bytes=int(round((package.quota_gb or 0) * GB)),
                                 duration_days=package.duration_days or 0,
                                 max_concurrent_sessions=package.max_concurrent_sessions)


def sale_snapshot(amount, *, admin_id=None, actor_admin_id=None, payment_method=None, payment_card_id=None,
                  discount_code=None, discount_amount=None, note=None) -> final.SaleSnapshot | None:
    if amount is None:
        return None
    return final.SaleSnapshot(amount=int(amount), admin_id=admin_id, actor_admin_id=actor_admin_id,
                              payment_method=payment_method, payment_card_id=payment_card_id,
                              discount_code=discount_code, discount_amount=discount_amount, note=note)


def panel_sale(db, user, package, actor_admin_id) -> final.SaleSnapshot | None:
    """What accounting.record_panel_sale would book, frozen (or None)."""
    from . import accounting
    if package is None:
        return None
    amount = accounting.sale_fallback_price(db, package, user.owner_admin_id)
    if amount <= 0:
        return None
    return sale_snapshot(amount, admin_id=user.owner_admin_id, actor_admin_id=actor_admin_id, note="فروش از پنل")


def payers(*, reseller_admin_id=None, reseller_cost=0, wallet_user_id=None, wallet_amount=0) -> list:
    """Reservation rows (Lifecycle 8). A zero amount has no row (5.3)."""
    rows = []
    if wallet_user_id is not None and wallet_amount > 0:
        rows.append(preparation.Payer(kind="customer_wallet", id=wallet_user_id, amount=int(wallet_amount)))
    if reseller_admin_id is not None and reseller_cost > 0:
        rows.append(preparation.Payer(kind="reseller_credit", id=reseller_admin_id, amount=int(reseller_cost)))
    return rows


def renewal(db, user: models.User, *, purchase: models.Purchase | None, add_gb: float = 0, add_days: int = 0,
            reset_usage: bool = False, package=None, sale: final.SaleSnapshot | None = None,
            reseller_cost: int = 0, reseller_admin_id=None, keep_package: bool = False,
            charge_wallet: bool = False, bulk: bool = False, key: str,
            business_key: str | None = None) -> preparation.Preparation:
    """renew_purchase (purchase given) or renew_user: one transaction (7.4).
    charge_wallet: the customer's wallet pays the sale, reserved in T1 (8);
    without it a wallet sale is refused - the money was taken elsewhere."""
    tenant = wallet_accounts.tenant_scope(db, user.owner_admin_id)
    wallet = charge_wallet and sale is not None and sale.payment_method == "wallet" and sale.amount > 0
    return preparation.Preparation(
        operation_type="renew_purchase" if purchase is not None else "renew_user",
        business_key=business_key or f"req:{tenant}:{key}", tenant_scope_key=tenant, target_user_id=user.id,
        intent=final.FinalIntent(user=user_snapshot(user), package=package_snapshot(package), sale=sale,
                                 purchase_id=purchase.id if purchase is not None else None,
                                 quota_bytes=int(round((add_gb or 0) * GB)), duration_days=int(add_days or 0),
                                 reset_usage=bool(reset_usage), keep_package=keep_package, bulk=bulk),
        payers=payers(reseller_admin_id=reseller_admin_id if reseller_admin_id is not None else user.owner_admin_id,
                      reseller_cost=reseller_cost,
                      wallet_user_id=user.id if wallet else None, wallet_amount=sale.amount if wallet else 0))


def package_slots(db, package, override=None) -> list:
    """The connection slots of a package sale (5.2 slot_key): the package's own
    connections by stable id, or the customer's hand-picked override."""
    speed = getattr(package, "speed_limit_mbps", None)
    if override is not None:
        return [preparation.Slot(slot_key=f"request_slot:{index}", node_id=int(spec["node_id"]),
                                 protocol=protocol_value(spec["protocol"]), flow=spec.get("flow") or "",
                                 max_concurrent_sessions=1, speed_limit_mbps=speed)
                for index, spec in enumerate(override)]
    return [preparation.Slot(slot_key=f"package_connection:{pc.id}", node_id=pc.node_id,
                             protocol=protocol_value(pc.protocol), flow=pc.flow or "", max_concurrent_sessions=1,
                             speed_limit_mbps=speed)
            for pc in sorted(package.connections, key=lambda row: row.id)]


def authorize_slots(db, slots, authorization_scope) -> None:
    """Every slot's node in the sanctioning authority's selling scope -
    the same check the legacy provision_connection makes per connection."""
    from fastapi import HTTPException
    from . import user_ops
    for slot in slots:
        node = db.get(models.Node, slot.node_id)
        if node is None:
            raise HTTPException(409, "node_unavailable")
        user_ops.authorize_node(db, node, authorization_scope)


def purchase(db, user: models.User, package, *, slots, comment=None, sale=None, reseller_cost=0,
             reseller_admin_id=None, count_delta=1, charge_wallet: bool = False,
             key: str) -> preparation.Preparation:
    """An existing customer buys a new, independent service (purchase);
    charge_wallet as in renewal()."""
    tenant = wallet_accounts.tenant_scope(db, user.owner_admin_id)
    wallet = charge_wallet and sale is not None and sale.payment_method == "wallet" and sale.amount > 0
    business_key = f"req:{tenant}:{key}"
    return preparation.Preparation(
        operation_type="purchase", business_key=business_key, tenant_scope_key=tenant,
        target_user_id=user.id,
        intent=final.FinalIntent(user=user_snapshot(user), package=package_snapshot(package), sale=sale,
                                 comment=comment, purchase_batch=batch_of(business_key),
                                 reseller_count_delta=count_delta),
        slots=slots,
        payers=payers(reseller_admin_id=reseller_admin_id if reseller_admin_id is not None else user.owner_admin_id,
                      reseller_cost=reseller_cost, wallet_user_id=user.id if wallet else None,
                      wallet_amount=sale.amount if wallet else 0))


def package_scope(db, package):
    """Who sanctions a bundled package's nodes: the package's own owner when
    package node scope is enforced (same as the legacy package paths)."""
    from . import user_ops
    if user_ops._package_node_scope_enforced(db):
        return user_ops.resolve_package_authorization_scope(package)
    return user_ops.NodeAuthorizationScope.unrestricted_scope()


def create_user(db, *, username, full_name=None, notes=None, telegram_id=None, owner_admin_id=None, package=None,
                quota_gb=0, expire_days=0, slots=(), comment=None, sale=None, reseller_cost=0,
                reseller_admin_id=None, count_delta=0, created_via=None, key: str,
                business_key: str | None = None, batch_fingerprint: str | None = None) -> preparation.Preparation:
    """A brand-new customer, optionally with a package and connections (7.5).
    With a package the service's limits come from the frozen package."""
    tenant = wallet_accounts.tenant_scope(db, owner_admin_id)
    business_key = business_key or f"req:{tenant}:{key}"
    return preparation.Preparation(
        operation_type="create_user", business_key=business_key, tenant_scope_key=tenant,
        intent=final.FinalIntent(
            user=final.UserSnapshot(username=username, full_name=full_name, notes=notes, telegram_id=telegram_id,
                                    owner_admin_id=owner_admin_id),
            package=package_snapshot(package), sale=sale, comment=comment,
            purchase_batch=batch_of(business_key) if slots else None,
            quota_bytes=0 if package is not None else int(round((quota_gb or 0) * GB)),
            duration_days=0 if package is not None else int(expire_days or 0),
            reseller_count_delta=count_delta, created_via=created_via, batch_fingerprint=batch_fingerprint),
        slots=list(slots),
        payers=payers(reseller_admin_id=reseller_admin_id if reseller_admin_id is not None else owner_admin_id,
                      reseller_cost=reseller_cost))


def bulk_fingerprint(*, prefix, count, package, quota_gb, expire_days, notes, connections) -> str:
    """What one bulk request asked for, frozen into every member's intent
    (and so its request_hash) under the frozen key bulk:{batch_token}:{index}:
    the same token with another prefix, count, package, notes, quota,
    duration or connections is another request (bulk_members: 409)."""
    import hashlib
    import json
    asked = dict(prefix=prefix, count=int(count), package_id=getattr(package, "id", None),
                 quota_gb=float(quota_gb or 0), expire_days=int(expire_days or 0), notes=notes or None,
                 connections=[[int(spec.node_id), protocol_value(spec.protocol),
                               int(getattr(spec, "max_concurrent_sessions", 1) or 1)] for spec in (connections or [])])
    canonical = json.dumps(asked, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def bulk_members(db, admin, batch_token: str, fingerprint: str) -> list:
    """The operations an earlier request with this batch token made - only
    if they are THIS admin's, in this admin's tenant, for THIS request (the
    fingerprint frozen in each member's intent); any other is a 409 before
    anything is resumed, reported or created. Called under bulk_lock."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from .. import models_provisioning as mp
    rows = db.execute(select(mp.ProvisioningOperation).where(
        mp.ProvisioningOperation.operation_type == "create_user",
        mp.ProvisioningOperation.business_key.like(f"bulk:{batch_token}:%"))
        .execution_options(populate_existing=True)).scalars().all()
    tenant = wallet_accounts.tenant_scope(db, admin.id)
    for row in rows:
        if row.actor_kind != "admin" or row.actor_id != admin.id or row.tenant_scope_key != tenant or (
                final._intent(row).batch_fingerprint != fingerprint):
            raise HTTPException(409, "provisioning_request_changed")
    return rows


BULK_LOCK_WAIT_SECONDS = 60.0


def bulk_lock(db, batch_token: str):
    """The batch token's ownership, taken before anything is read or created
    and held for the whole batch: two requests with the same token never run
    side by side, so the second one sees the first one's members (and their
    fingerprint) and is answered or refused - never a second batch. A lock
    file of the installation: durable operations only ever run on the host
    that owns it (T1 checks the ownership). Fail-closed if it cannot be used."""
    import hashlib
    import os
    from fastapi import HTTPException
    from .. import models_provisioning as mp
    from . import gate_locks
    installation = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    # No transaction is held while waiting, and the members are then read in
    # a NEW one: a snapshot from before the wait would not show the batch
    # the other request just made (MariaDB REPEATABLE READ).
    db.rollback()
    bucket = hashlib.sha256(batch_token.encode()).hexdigest()[:2]
    lock = gate_locks.FileLock(os.path.join(gate_locks.installation_dir(installation), f"bulk-{bucket}.lock"))
    try:
        return lock.acquire(shared=False, timeout=BULK_LOCK_WAIT_SECONDS)
    except gate_locks.LockTimeout:
        raise HTTPException(409, {"error": "operation_in_progress", "state": "bulk_batch"}) from None
    except (gate_locks.LockUnavailable, ValueError):
        raise HTTPException(503, "bulk_lock_unavailable") from None


def bulk_create(db, admin, *, prefix, count, package=None, quota_gb=0, expire_days=None, notes=None,
                connections=None, batch_token: str, before_new=None) -> dict:
    """Panel bulk creation as N independent create_user operations (7.5):
    business key bulk:{batch_token}:{index}; every member that succeeds is
    complete (its own reservation captured), every one that fails left
    nothing. No batch charge and no batch refund. Same result shape and the
    same name skipping as the legacy bulk_create_users. The same token again
    resumes/reports its members only for the same admin, tenant and request
    (bulk_members); anything else is a 409."""
    fingerprint = bulk_fingerprint(prefix=prefix, count=count, package=package, quota_gb=quota_gb,
                                   expire_days=expire_days, notes=notes, connections=connections)
    with bulk_lock(db, batch_token):
        if not bulk_members(db, admin, batch_token, fingerprint) and before_new is not None:
            before_new()                           # checks only a NEW batch needs
        return _bulk_members_run(db, admin, prefix=prefix, count=count, package=package, quota_gb=quota_gb,
                                 expire_days=expire_days, notes=notes, connections=connections,
                                 batch_token=batch_token, fingerprint=fingerprint)


def _bulk_members_run(db, admin, *, prefix, count, package, quota_gb, expire_days, notes, connections,
                      batch_token, fingerprint) -> dict:
    from types import SimpleNamespace
    from fastapi import HTTPException
    from . import admin_billing, provisioning_durable, user_ops
    created, skipped, pending = [], [], []
    if package is not None:
        slots = package_slots(db, package)
        authorize_slots(db, slots, package_scope(db, package))
    else:
        owner = db.get(models.AdminUser, admin.id)
        scope = (user_ops.NodeAuthorizationScope.unrestricted_scope() if owner is None or owner.is_superadmin
                 else user_ops.NodeAuthorizationScope.scoped(owner.id))
        slots = package_slots(db, None, [{"node_id": spec.node_id, "protocol": spec.protocol, "flow": ""}
                                         for spec in (connections or [])])
        for index, spec in enumerate(connections or []):
            slots[index] = slots[index].copy(update={"max_concurrent_sessions": getattr(spec, "max_concurrent_sessions", 1) or 1})
        authorize_slots(db, slots, scope)
    factory = session_factory_for(db)
    index, attempts, cap = 1, 0, count * 5 + 20
    def unfinished(exc):
        return exc.status_code == 409 and isinstance(exc.detail, dict) and exc.detail.get("error") == "operation_in_progress"

    # A member still in progress keeps its place: it may yet complete, so no
    # other index is created (and charged) in its stead.
    while len(created) + len(pending) < count and attempts < cap:
        attempts += 1
        username = f"{prefix}{index}"
        member = index
        index += 1
        business_key = f"bulk:{batch_token}:{member}"
        previous = existing_operation(db, "create_user", business_key)
        if previous is not None:
            # A retry of the same batch token: this index IS that member -
            # resumed or reported, never replaced by a new name or charged again.
            username = final._intent(previous).user.username
            if previous.state == "compensated":
                skipped.append({"name": username, "reason": previous.error_code or "provisioning_failed"})
                continue
            db.rollback()
            try:
                if previous.state != "completed":
                    provisioning_durable.resume(factory, previous.id)
                created.append(username)
            except HTTPException as exc:
                if unfinished(exc):
                    pending.append(username)
                    continue
                detail = exc.detail.get("error") if isinstance(exc.detail, dict) else exc.detail
                skipped.append({"name": username, "reason": str(detail)})
            continue
        if db.query(models.User.id).filter(models.User.username == username).first() or (
                provisioning_durable.username_claimed(db, username)):
            skipped.append({"name": username, "reason": "این نام کاربری قبلا وجود دارد"})
            continue
        buyer = SimpleNamespace(owner_admin_id=admin.id)
        request = create_user(
            db, username=username, notes=notes, owner_admin_id=admin.id, package=package,
            quota_gb=0 if package else (quota_gb or 0), expire_days=0 if package else (expire_days or 0),
            slots=slots, sale=panel_sale(db, buyer, package, admin.id),
            reseller_cost=admin_billing.package_cost(admin, package, units=1) if package is not None else 0,
            reseller_admin_id=admin.id, key="", business_key=business_key, batch_fingerprint=fingerprint)
        db.rollback()
        try:
            provisioning_durable.run(factory, request, actor_kind="admin", actor_id=admin.id)
            created.append(username)
        except HTTPException as exc:
            if unfinished(exc):
                pending.append(username)
                continue
            detail = exc.detail.get("error") if isinstance(exc.detail, dict) else exc.detail
            skipped.append({"name": username, "reason": str(detail)})
            if exc.status_code == 400 and "اعتبار" in str(detail):
                break                              # the reseller's credit ran out: the rest would fail the same way
    return {"created": created, "created_count": len(created), "skipped": skipped, "skipped_count": len(skipped),
            "in_progress": pending}


def bulk_renew(db, admin, targets, *, package=None, add_gb=0, add_days=0, reset_usage=False,
               batch_token: str) -> dict:
    """Panel bulk renewal as N independent renew_purchase operations, one per
    (customer, service) target (business key bulk_renew:{token}:{purchase}):
    each reserves and captures its own credit; a failed one leaves nothing.
    No batch charge and no batch refund (same rule as bulk creation, 7.5)."""
    from fastapi import HTTPException
    from . import admin_billing, provisioning_durable
    factory = session_factory_for(db)
    updated, failed = 0, []
    for user, purchase in targets:
        request = renewal(db, user, purchase=purchase, add_gb=add_gb, add_days=add_days, reset_usage=reset_usage,
                          package=package, sale=panel_sale(db, user, package, admin.id) if package else None,
                          reseller_cost=admin_billing.renewal_cost(admin, package, add_gb or 0),
                          reseller_admin_id=admin.id, bulk=True, key=batch_token,
                          business_key=f"bulk_renew:{batch_token}:{purchase.id}")
        user_id, purchase_id = user.id, purchase.id
        db.rollback()
        try:
            provisioning_durable.run(factory, request, actor_kind="admin", actor_id=admin.id)
            updated += 1
        except HTTPException as exc:
            failed.append({"user_id": user_id, "purchase_id": purchase_id,
                           "reason": exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")})
    return {"updated_count": updated, "failed": failed}



# ------------------------------------------------------------------ replay before preflight (Lifecycle 13)
def header_key(request_header):
    """The caller's own idempotency key, or None (no header: every request is
    new, as today - there is nothing to replay)."""
    from . import provisioning_durable
    if not isinstance(request_header, str) or not request_header.strip():
        return None
    return provisioning_durable.idempotency_key(request_header)


def prior_operation(db, operation_type: str, tenant_scope_key: str, request_header):
    """The operation an earlier request with the same key already made, in
    this tenant - found BEFORE any check that only a NEW operation needs
    (one-time/trial package, target existence): a retry of a completed
    request is answered with its result, never refused by its own effect."""
    key = header_key(request_header)
    if key is None:
        return None
    return existing_operation(db, operation_type, f"req:{tenant_scope_key}:{key}")


def prior_delete(db, resource_kind: str, request_header, *, resource_id=None, target_username=None):
    """The deletion an earlier request with the same key made. With the
    resource id (path endpoints) the business key is exact. The bot's
    delete-by-username has no id left after the deletion: the key is matched
    by its suffix and the earlier request's hash recomputed with THIS
    request's name - the same key for another customer is a 409, never the
    earlier {ok: true}. The caller then checks actor and tenant."""
    from fastapi import HTTPException
    from sqlalchemy import select
    from .. import models_provisioning as mp
    key = header_key(request_header)
    if key is None:
        return None
    query = select(mp.ProvisioningOperation).where(mp.ProvisioningOperation.operation_type == f"delete_{resource_kind}")
    if resource_id is not None:
        query = query.where(mp.ProvisioningOperation.business_key == f"delete:{resource_kind}:{resource_id}:{key}")
        return db.execute(query).scalar_one_or_none()
    rows = db.execute(query.where(mp.ProvisioningOperation.business_key.like(
        f"delete:{resource_kind}:%:{key}"))).scalars().all()
    if not rows:
        return None
    for row in rows:
        same = delete_preparation.DeleteRequest(
            resource_kind=resource_kind, resource_id=row.target_user_id or 1, business_key=row.business_key,
            tenant_scope_key=row.tenant_scope_key, target_username=target_username)
        if row.target_user_id is not None and delete_preparation.request_digest(same) == row.request_hash:
            return row
    raise HTTPException(409, "provisioning_request_changed")


def replay(db, request, request_header, *, actor=None):
    """The operation an earlier request with the same key made for THIS
    request (Lifecycle 5.1, 13), checked against the whole request before its
    result is returned: the same business key with another request - another
    package, price, target, quota, duration or connections - is a 409, never
    the earlier result. None: no key, or no earlier operation (a new request,
    whose own preflights run next). actor=(kind, id): the earlier operation
    must also be that actor's, checked before anything is resumed."""
    from fastapi import HTTPException
    if header_key(request_header) is None:
        return None
    previous = existing_operation(db, request.operation_type, request.business_key)
    if previous is None:
        return None
    if previous.request_hash != preparation.request_digest(request) or (
            previous.tenant_scope_key != request.tenant_scope_key) or (
            actor is not None and (previous.actor_kind, previous.actor_id) != actor):
        raise HTTPException(409, "provisioning_request_changed")
    return settle(db, previous)


def settle(db, operation):
    """The result of a replayed operation: completed as it is; an unfinished
    one advanced from this request (same budget and answers as run())."""
    from fastapi import HTTPException
    from . import provisioning_durable
    if operation.state == "completed":
        return operation
    if operation.state == "compensated":
        raise HTTPException(400, {"error": operation.error_code or "provisioning_failed", "operation_id": operation.id})
    factory = session_factory_for(db)
    operation_id = operation.id
    db.rollback()
    result = provisioning_durable.resume(factory, operation_id)
    db.expire_all()
    return result
