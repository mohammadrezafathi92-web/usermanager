"""Standalone acceptance checks for mean proration and actual-charge snapshots."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import models
from app.database import Base
from app.services import admin_billing
from app.services.reseller_refund import bind_sale, calculate_refund, preview


def check_math():
    result = calculate_refund(100000, used_bytes=30, quota_bytes=100,
                              elapsed_seconds=20, duration_seconds=100)
    assert result["refund_amount"] == 75000
    assert result["consumed_percent"] == 25
    # Symmetric mean, monotonic consumption, and exact accounting, without
    # floating-point drift even beyond the largest exact float integer.
    huge = 10**18 + 1
    for time in range(0, 121, 10):
        previous = huge
        for volume in range(0, 121, 10):
            a = calculate_refund(huge, used_bytes=volume, quota_bytes=100,
                                 elapsed_seconds=time, duration_seconds=100)
            b = calculate_refund(huge, used_bytes=time, quota_bytes=100,
                                 elapsed_seconds=volume, duration_seconds=100)
            assert a["refund_amount"] == b["refund_amount"]
            assert 0 <= a["refund_amount"] <= previous
            assert a["consumed_amount"] + a["refund_amount"] == huge
            previous = a["refund_amount"]
    for time, volume, expected in [(0, 0, 100000), (100, 100, 0),
                                    (200, 10, 45000), (10, 200, 45000)]:
        assert calculate_refund(100000, used_bytes=volume, quota_bytes=100,
                                elapsed_seconds=time, duration_seconds=100)["refund_amount"] == expected
    assert calculate_refund(1, used_bytes=1, quota_bytes=3,
                            elapsed_seconds=1, duration_seconds=3)["refund_amount"] == 0
    assert calculate_refund(100, used_bytes=50, quota_bytes=100,
                            elapsed_seconds=0, duration_seconds=0)["refund_amount"] == 50
    assert calculate_refund(100, used_bytes=0, quota_bytes=0,
                            elapsed_seconds=50, duration_seconds=100)["refund_amount"] == 50
    for values in [(0, 0, 0, 0), (-1, 100, 0, 100)]:
        try:
            calculate_refund(100, used_bytes=values[0], quota_bytes=values[1],
                             elapsed_seconds=values[2], duration_seconds=values[3])
        except ValueError:
            pass
        else:
            raise AssertionError("invalid/unmetered inputs accepted")


def check_snapshot():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = models.AdminUser(username="refund-test", hashed_password="unused",
                                 balance=200000, is_superadmin=False, billing_mode="flat")
        user = models.User(username="refund-customer")
        package = models.Package(name="test", price=100000, cooperation_price=100000,
                                 quota_gb=1, duration_days=10)
        db.add_all([admin, user, package])
        db.commit()
        debit = admin_billing.charge_for_package(db, admin, package)
        assert debit.kind == "admin_credit_spend" and debit.amount == 100000
        now = datetime(2026, 10, 6)
        purchase = models.Purchase(user_id=user.id, package_id=package.id,
                                   quota_bytes=100, used_bytes=30,
                                   expire_at=now + timedelta(days=8))
        db.add(purchase)
        db.flush()
        bind_sale(db, purchase, debit, package)
        db.commit()
        package.price = package.cooperation_price = 999999
        db.commit()
        result = preview(db, purchase, now=now)
        assert result["refund_amount"] == 75000
        assert result["refund_admin_id"] == admin.id
        assert result["estimate_only"] and not result["execution_available"]
        db.refresh(admin)
        assert admin.balance == 100000, "preview moved money"
        purchase.quota_bytes = 200
        db.commit()
        try:
            preview(db, purchase, now=now)
        except HTTPException as exc:
            assert exc.status_code == 409 and exc.detail == "reseller_entitlement_changed"
        else:
            raise AssertionError("changed entitlement accepted")
        old = models.Purchase(user_id=user.id, quota_bytes=100)
        db.add(old)
        db.flush()
        try:
            preview(db, old)
        except HTTPException as exc:
            assert exc.detail == "reseller_charge_unproven"
        else:
            raise AssertionError("historical price guessed")


if __name__ == "__main__":
    check_math()
    check_snapshot()
    print("PASS: reseller mean proration and immutable charge preview")
