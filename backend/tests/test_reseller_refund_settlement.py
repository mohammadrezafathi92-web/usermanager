"""No network: transaction/retry safety before wiring a remote worker."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError, OperationalError

from app import models
from app.database import Base
from app.services.reseller_refund import bind_sale
from app.services.reseller_refund_settlement import prepare, capture_counters, verify_stopped, settle
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step


def refused(call, code):
    try:
        call()
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)
    else:
        raise AssertionError("unsafe settlement accepted")


def run(engine):
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        admin = models.AdminUser(username="charged", hashed_password="unused", balance=0,
                                 is_superadmin=False, role="seller", tree_path="/1/")
        other_admin = models.AdminUser(username="other", hashed_password="unused", balance=0,
                                       is_superadmin=False, role="seller", tree_path="/2/")
        node = models.Node(name="fake", type=models.NodeType.xray)
        package = models.Package(name="refund", price=100000, quota_gb=1, duration_days=10)
        db.add_all([admin, other_admin, node, package])
        db.flush()
        user = models.User(username="customer", owner_admin_id=admin.id, balance=1234)
        db.add(user)
        db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=100,
                                   used_bytes=20, expire_at=datetime.utcnow() + timedelta(days=8))
        healthy = models.Purchase(user_id=user.id, quota_bytes=1000)
        db.add_all([purchase, healthy])
        db.flush()
        conn = models.Connection(user_id=user.id, node_id=node.id, purchase_id=purchase.id,
                                  type=models.ConnectionType.xray, total_bytes=20, last_rx_bytes=20)
        good_conn = models.Connection(user_id=user.id, node_id=node.id, purchase_id=healthy.id,
                                       type=models.ConnectionType.xray)
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=admin.id)
        db.add_all([conn, good_conn, debit])
        db.flush()
        bind_sale(db, purchase, debit, package)
        db.add(models.UsageLog(user_id=user.id, connection_id=conn.id, delta_bytes=20))
        db.commit()
        pid, cid, uid, hid, gcid = purchase.id, conn.id, user.id, healthy.id, good_conn.id
        refused(lambda: prepare(db, purchase_id=pid, actor=other_admin), "service_not_found")
        operation = prepare(db, purchase_id=pid, actor=admin)
        assert prepare(db, purchase_id=pid, actor=admin) == operation
        refused(lambda: settle(db, operation_id=operation), "refund_remote_unverified")
        assert db.get(models.AdminUser, admin.id).balance == 0
        refused(lambda: verify_stopped(db, operation_id=operation, connection_id=cid,
                                      final_total_bytes=19), "refund_usage_unverified")
        refused(lambda: capture_counters(db, operation_id=operation, connection_id=cid,
                                        download_bytes=10, upload_bytes=0), "refund_counter_reset")
        captured = capture_counters(db, operation_id=operation, connection_id=cid,
                                    download_bytes=30, upload_bytes=0)
        assert captured["total_bytes"] == 30
        assert capture_counters(db, operation_id=operation, connection_id=cid,
                                download_bytes=30, upload_bytes=0) == captured
        refused(lambda: settle(db, operation_id=operation), "refund_remote_unverified")
        assert db.get(models.AdminUser, admin.id).balance == 0
        refused(lambda: capture_counters(db, operation_id=operation, connection_id=cid,
                                        download_bytes=31, upload_bytes=0), "refund_usage_changed")
        verify_stopped(db, operation_id=operation, connection_id=cid, final_total_bytes=30)
        # Identity changes invalidate evidence, even with the same DB id.
        original = db.get(models.Connection, cid)
        original.xr_uuid = "different-credential"
        db.commit()
        refused(lambda: settle(db, operation_id=operation), "refund_resource_changed")
        original.xr_uuid = None
        db.commit()
        active = models.RadiusActiveSession(connection_id=cid, session_id="not-stopped")
        db.add(active)
        db.commit()
        refused(lambda: settle(db, operation_id=operation), "refund_sessions_active")
        db.delete(active)
        db.commit()
        # SQL CHECKs must reject NULL-shaped pseudo-verification/results.
        for stmt in [Step.__table__.update().where(Step.operation_id == operation).values(final_total_bytes=None),
                     Operation.__table__.update().where(Operation.id == operation).values(
                         state="completed", completed_at=datetime.utcnow(), refund_entry_id=1, refund_amount=None)]:
            try:
                db.execute(stmt)
                db.commit()
            except (IntegrityError, OperationalError) as exc:
                if isinstance(exc, OperationalError):
                    assert engine.dialect.name in ("mysql", "mariadb") and exc.orig.args[0] == 4025, exc
                db.rollback()
            else:
                raise AssertionError("database accepted an incomplete result")
        # Inject a final-commit crash: every financial and deletion write rolls back.
        def fail_commit(session):
            raise RuntimeError("injected final commit failure")
        event.listen(db, "before_commit", fail_commit)
        try:
            settle(db, operation_id=operation)
        except RuntimeError as exc:
            assert str(exc) == "injected final commit failure"
        else:
            raise AssertionError("crash not injected")
        finally:
            event.remove(db, "before_commit", fail_commit)
        assert db.get(models.AdminUser, admin.id).balance == 0
        assert db.get(models.Purchase, pid) and db.get(models.Connection, cid)
        assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count() == 0
        db.rollback()
        def finish(_):
            with Session(engine) as competing:
                return settle(competing, operation_id=operation)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(finish, range(2)))
        assert results[0] == results[1], "concurrent attempts returned different settlements"
        result = results[0]
        assert 74999 <= result["refund_amount"] <= 75000
        assert settle(db, operation_id=operation) == result
        assert prepare(db, purchase_id=pid, actor=admin) == operation
        assert db.get(models.AdminUser, admin.id).balance == result["refund_amount"]
        assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count() == 1
        assert db.get(models.Purchase, pid) is None and db.get(models.Connection, cid) is None
        assert db.get(models.User, uid).balance == 1234
        assert db.get(models.Purchase, hid) and db.get(models.Connection, gcid)
        assert db.query(models.UsageLog).one().connection_id is None
        assert db.get(models.AdminUser, other_admin.id).balance == 0


if __name__ == "__main__":
    with TemporaryDirectory(prefix="um-refund-", dir="/tmp") as directory:
        engine = create_engine(f"sqlite:///{directory}/refund.db")
        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        run(engine)
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL")
    if url:
        import _mariadb_scratch as scratch
        engine = scratch.claim(url)
        try:
            run(engine)
        finally:
            scratch.release(engine)
    elif os.environ.get("CI"):
        raise SystemExit("MariaDB URL is required in CI")
    else:
        print("MariaDB not run locally")
    print("PASS: refund verification, atomic rollback, retry, ownership, isolated deletion")
