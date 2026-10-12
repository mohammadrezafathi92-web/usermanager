"""Bulk grants choose independent services before charging reseller credit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

import _no_network
_no_network.install([])

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import users as users_router
from app.services import admin_billing, user_ops


GB = 1024 ** 3
engine = create_engine("sqlite://")
models.Base.metadata.create_all(engine)
Factory = sessionmaker(bind=engine)

with Factory() as db:
    admin = models.AdminUser(username="bulk-owner", hashed_password="x", balance=1_000_000)
    package = models.Package(name="bulk-plan", quota_gb=12, duration_days=30,
                             price=200_000, cooperation_price=80_000)
    db.add_all([admin, package])
    db.flush()
    package.owner_admin_id = admin.id
    one = models.User(username="one", owner_admin_id=admin.id,
                      total_quota_bytes=99 * GB, used_bytes=7 * GB)
    many = models.User(username="many", owner_admin_id=admin.id,
                       total_quota_bytes=88 * GB, used_bytes=5 * GB)
    legacy = models.User(username="legacy", owner_admin_id=admin.id,
                         total_quota_bytes=6 * GB, used_bytes=GB)
    racing = models.User(username="racing", owner_admin_id=admin.id)
    db.add_all([one, many, legacy, racing])
    db.flush()
    node = models.Node(name="scratch", type=models.NodeType.mikrotik)
    db.add(node)
    db.flush()
    db.add_all([
        models.Purchase(user_id=one.id, quota_bytes=2 * GB, used_bytes=GB),
        models.Purchase(user_id=many.id, quota_bytes=3 * GB, used_bytes=GB),
        models.Purchase(user_id=many.id, quota_bytes=4 * GB, used_bytes=GB),
        models.Purchase(user_id=racing.id, quota_bytes=5 * GB, used_bytes=GB),
        models.Connection(user_id=legacy.id, node_id=node.id,
                          type=models.ConnectionType.l2tp,
                          ppp_username="legacy", ppp_password="x"),
    ])
    db.commit()
    one_id, many_id, legacy_id, racing_id, admin_id, package_id = (
        one.id, many.id, legacy.id, racing.id, admin.id, package.id)
    one_purchase = one.purchases[0].id
    many_purchases = sorted(p.id for p in many.purchases)

with Factory() as db:
    admin = db.get(models.AdminUser, admin_id)
    choices = users_router.bulk_purchase_targets(
        schemas.BulkPurchaseTargetsRequest(user_ids=[one_id, many_id]), db=db, admin=admin)
    assert [len(item["purchases"]) for item in choices] == [1, 2]
    before = admin.balance
    try:
        users_router.bulk_update_users(
            schemas.BulkUpdateUsersRequest(user_ids=[one_id, many_id], package_id=package_id),
            db=db, admin=admin)
    except HTTPException as exc:
        assert (exc.status_code, exc.detail) == (409, "service_selection_required")
    else:
        raise AssertionError("ambiguous batch accepted")
    db.refresh(admin)
    assert admin.balance == before
    assert db.query(models.LedgerEntry).count() == 0
    assert db.get(models.Purchase, one_purchase).quota_bytes == 2 * GB
    try:
        users_router.bulk_update_users(
            schemas.BulkUpdateUsersRequest(user_ids=[legacy_id], package_id=package_id),
            db=db, admin=admin)
    except HTTPException as exc:
        assert (exc.status_code, exc.detail) == (503, "legacy_service_migration_required")
    else:
        raise AssertionError("legacy service was charged/edited")
    db.refresh(admin)
    assert admin.balance == before
    assert db.get(models.User, legacy_id).total_quota_bytes == 6 * GB

with Factory() as db:
    admin = db.get(models.AdminUser, admin_id)
    result = users_router.bulk_update_users(
        schemas.BulkUpdateUsersRequest(
            user_ids=[one_id, many_id], package_id=package_id,
            purchase_ids={many_id: many_purchases[1]}), db=db, admin=admin)
    assert result == {"updated_count": 2}
    assert db.get(models.Purchase, one_purchase).quota_bytes == 12 * GB
    assert db.get(models.Purchase, many_purchases[1]).quota_bytes == 12 * GB
    assert db.get(models.Purchase, many_purchases[0]).quota_bytes == 3 * GB
    assert db.get(models.User, one_id).total_quota_bytes == 99 * GB
    assert db.get(models.User, many_id).total_quota_bytes == 88 * GB
    db.refresh(admin)
    assert admin.balance == before - 160_000
    sales = db.query(models.LedgerEntry).filter_by(kind="sale_renew").all()
    assert {sale.purchase_id for sale in sales} == {one_purchase, many_purchases[1]}

with Factory() as db:
    admin = db.get(models.AdminUser, admin_id)
    before_race = admin.balance
    real_charge = admin_billing.charge_for_package
    def add_service_after_charge(db_, admin_, package_, units=1):
        debit = real_charge(db_, admin_, package_, units=units)
        db_.add(models.Purchase(user_id=racing_id, quota_bytes=GB))
        db_.commit()
        return debit
    admin_billing.charge_for_package = add_service_after_charge
    try:
        try:
            users_router.bulk_update_users(
                schemas.BulkUpdateUsersRequest(user_ids=[racing_id], package_id=package_id),
                db=db, admin=admin)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (409, "service_selection_required")
        else:
            raise AssertionError("changed target was accepted after charge")
    finally:
        admin_billing.charge_for_package = real_charge
    db.refresh(admin)
    assert admin.balance == before_race
    assert db.query(models.LedgerEntry).filter_by(kind="sale_renew", user_id=racing_id).count() == 0

with Factory() as db:
    admin = db.get(models.AdminUser, admin_id)
    try:
        user_ops.resolve_bulk_purchase_targets(
            db, [one_id], {one_id: many_purchases[1]}, admin)
    except HTTPException as exc:
        assert (exc.status_code, exc.detail) == (409, "service_selection_invalid")
    else:
        raise AssertionError("cross-customer service selection accepted")

assert _no_network.attempts == []
print("PASS bulk purchase target selection and pre-charge refusal")
