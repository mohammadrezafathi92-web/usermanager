"""Settlement rechecks durable evidence; prepared intents recheck new holds.

No NAS/network. Earlier worker/fake-packet review established the late-marker
race; these regressions exercise its final transaction directly, not a mock
of the settlement decision. Each fixture is a new local SQLite database.
"""
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-final-boundary-only-01234567890123456789")
os.environ.setdefault("RADIUS_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _no_network
_no_network.install()

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models
from app.database import Base
from app.config import settings
from app.models_provisioning import ProvisioningOperation
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from app.services import reseller_refund_settlement as settlement
from app.services import reseller_refund_worker as worker
from app.services.reseller_refund import bind_sale
from app.services.reseller_refund_block import note_unverified

failures = []

def check(name, actual, expected):
    print("PASS" if actual == expected else "FAIL", name, actual)
    if actual != expected:
        failures.append(name)

def fixture(folder, name):
    engine = create_engine(f"sqlite:///{folder}/{name}.db")
    Base.metadata.create_all(engine)
    ProvisioningOperation.__table__.create(engine)
    db = Session(engine)
    actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", balance=0)
    node = models.Node(name="test", type=models.NodeType.mikrotik, mt_host="10.20.0.1")
    package = models.Package(name="test", price=100000, duration_days=10, quota_gb=1)
    db.add_all([actor, node, package]); db.flush()
    user = models.User(username="test", owner_admin_id=actor.id, status=models.UserStatus.active)
    db.add(user); db.flush()
    purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=1000,
                               used_bytes=0, expire_at=dt.datetime.utcnow()+dt.timedelta(days=10))
    db.add(purchase); db.flush()
    connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
                                    type=models.ConnectionType.openvpn, ppp_username="test-ovpn", ppp_password="unused")
    db.add(connection); db.flush()
    debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
    sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id,
                              user_id=user.id, purchase_id=purchase.id)
    db.add_all([debit, sale]); db.flush()
    bind_sale(db, purchase, debit, package, sale_entry=sale)
    db.commit()
    ids = purchase.id, connection.id, user.id, actor.id
    opid = settlement.prepare(db, purchase_id=ids[0], actor=actor)
    return engine, db, ids, opid

def open_operation(db, uid):
    op = ProvisioningOperation(operation_type="renew_purchase", business_key="test", request_hash="0"*64,
        tenant_scope_key="test", actor_kind="admin", intent="{}", target_user_id=uid, state="prepared",
        forward_deadline=dt.datetime.utcnow()+dt.timedelta(hours=1), wallet_epoch_at_start=0)
    db.add(op); db.commit()
    return op.id

def outcome(fn):
    try:
        return fn()
    except HTTPException as exc:
        return exc.detail

with tempfile.TemporaryDirectory(prefix="um-final-boundary-") as folder, \
        patch.dict(os.environ, {"UM_LOCK_DIR": folder}), \
        patch.object(settings, "reseller_cancellation_enabled", True):
    engine, db, ids, opid = fixture(folder, "late")
    worker._start_mutation(db, opid)
    settlement.block_ppp(db, operation_id=opid, connection_id=ids[1])
    settlement.capture_ppp(db, operation_id=opid, connection_id=ids[1])
    settlement.verify_stopped(db, operation_id=opid, connection_id=ids[1], final_total_bytes=0)
    # This exact marker is committed by authentic but unbound accounting.
    note_unverified(db, ids[1]); db.commit()
    check("late evidence refuses settlement", outcome(lambda: settlement.settle(db, operation_id=opid)),
          "ppp_session_unverified")
    check("late evidence creates no refund", db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count(), 0)
    check("late evidence retains service", db.get(models.Purchase, ids[0]) is not None, True)
    db.close(); engine.dispose()

    engine, db, ids, opid = fixture(folder, "prepared")
    other = open_operation(db, ids[2])
    check("prepared resume rechecks new operation", outcome(lambda: worker._start_mutation(db, opid)),
          "reseller_operation_pending")
    check("refusal does not claim mutation", db.get(Operation, opid).mutation_started_at, None)
    db.rollback()
    pending = db.get(ProvisioningOperation, other)
    pending.state, pending.completed_at = "completed", dt.datetime.utcnow()
    db.commit()
    worker._start_mutation(db, opid)
    check("resolved hold permits first mutation", db.get(Operation, opid).mutation_started_at is not None, True)
    db.rollback()
    pending = db.get(ProvisioningOperation, other)
    pending.state, pending.completed_at = "prepared", None
    db.commit()
    check("real started resume is not rejudged", outcome(lambda: worker._start_mutation(db, opid)), None)
    db.close(); engine.dispose()

    engine, db, ids, opid = fixture(folder, "valid")
    worker._start_mutation(db, opid)
    settlement.block_ppp(db, operation_id=opid, connection_id=ids[1])
    settlement.capture_ppp(db, operation_id=opid, connection_id=ids[1])
    settlement.verify_stopped(db, operation_id=opid, connection_id=ids[1], final_total_bytes=0)
    result = settlement.settle(db, operation_id=opid)
    check("valid final proof completes", result["state"], "completed")
    check("retry returns stored result", settlement.settle(db, operation_id=opid), result)
    check("refund exactly once", db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count(), 1)
    db.close(); engine.dispose()

if failures:
    raise SystemExit(f"{len(failures)} FAILED")
print("all final-boundary checks passed")
