"""The customer-list reset may only sell and reset one independent service."""
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


GB = 1024 ** 3
engine = create_engine("sqlite://")
models.Base.metadata.create_all(engine)
Factory = sessionmaker(bind=engine)

with Factory() as db:
    admin = models.AdminUser(username="reset-owner", hashed_password="x", balance=1_000_000)
    package = models.Package(name="reset-plan", quota_gb=10, duration_days=30,
                             price=200_000, cooperation_price=80_000)
    node = models.Node(name="scratch", type=models.NodeType.mikrotik)
    db.add_all([admin, package, node])
    db.flush()
    package.owner_admin_id = admin.id
    one = models.User(username="reset-one", owner_admin_id=admin.id,
                      total_quota_bytes=19 * GB, used_bytes=7 * GB)
    many = models.User(username="reset-many", owner_admin_id=admin.id,
                       total_quota_bytes=29 * GB, used_bytes=8 * GB)
    empty = models.User(username="reset-empty", owner_admin_id=admin.id)
    legacy = models.User(username="reset-legacy", owner_admin_id=admin.id)
    db.add_all([one, many, empty, legacy])
    db.flush()
    first = models.Purchase(user_id=one.id, quota_bytes=10 * GB, used_bytes=3 * GB)
    second = models.Purchase(user_id=many.id, quota_bytes=10 * GB, used_bytes=4 * GB)
    third = models.Purchase(user_id=many.id, quota_bytes=10 * GB, used_bytes=5 * GB)
    db.add_all([first, second, third,
                models.Connection(user_id=legacy.id, node_id=node.id,
                                  type=models.ConnectionType.l2tp,
                                  ppp_username="legacy", ppp_password="x")])
    db.commit()
    admin_id, package_id = admin.id, package.id
    one_id, many_id, empty_id, legacy_id = one.id, many.id, empty.id, legacy.id
    first_id, second_id, third_id = first.id, second.id, third.id

with Factory() as db:
    admin = db.get(models.AdminUser, admin_id)
    before = admin.balance
    targets = users_router.reset_usage_targets(many_id, db=db, admin=admin)
    assert not targets["has_legacy"]
    assert [item["id"] for item in targets["purchases"]] == [second_id, third_id]
    assert users_router.reset_usage_targets(legacy_id, db=db, admin=admin)["has_legacy"]
    for user_id, purchase_id, code, status in (
        (many_id, None, "service_selection_required", 409),
        (many_id, first_id, "service_selection_invalid", 409),
        (empty_id, None, "service_purchase_required", 409),
        (legacy_id, None, "legacy_service_migration_required", 503),
    ):
        try:
            users_router.reset_usage(user_id, schemas.ResetUsageRequest(
                package_id=package_id, purchase_id=purchase_id), db=db, admin=admin)
        except HTTPException as exc:
            assert (exc.status_code, exc.detail) == (status, code)
        else:
            raise AssertionError(f"unsafe reset accepted: {user_id}")
        db.rollback()
        db.refresh(admin)
        assert admin.balance == before
        assert db.query(models.LedgerEntry).count() == 0

    users_router.reset_usage(one_id, schemas.ResetUsageRequest(package_id=package_id), db=db, admin=admin)
    assert db.get(models.Purchase, first_id).used_bytes == 0
    assert db.get(models.User, one_id).used_bytes == 7 * GB
    first_balance = admin.balance
    assert first_balance < before

    users_router.reset_usage(many_id, schemas.ResetUsageRequest(
        package_id=package_id, purchase_id=third_id), db=db, admin=admin)
    assert db.get(models.Purchase, second_id).used_bytes == 4 * GB
    assert db.get(models.Purchase, third_id).used_bytes == 0
    assert db.get(models.User, many_id).used_bytes == 8 * GB
    assert admin.balance < first_balance
    entries = db.query(models.LedgerEntry).filter_by(kind="sale_renew").all()
    assert {entry.purchase_id for entry in entries} == {first_id, third_id}

assert _no_network.attempts == []
print("PASS panel reset charges and resets only one selected Purchase")
