"""Atomic refund/claim/terminal operation rollback on SQLite and real CI MariaDB."""
import datetime as dt
import os
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import provisioning_schema, receipt_void_schema, wallet_accounts
from app.services import payment_reservations as payments, provisioning_transitions as transitions
from app.services import provisioning_compensation as compensation


def refusal(code, callback):
    try:
        callback()
        raise AssertionError("unexpected success")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    user = wallet_accounts.create_user_with_wallet(db, username="payer", balance=100)
    db.commit()
    uid = user.id
    operation = mp.ProvisioningOperation(operation_type="purchase", business_key=str(uuid.uuid4()),
        request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}",
        target_user_id=uid, username_claim="held-name", state="prepared",
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
    db.add(operation)
    db.flush()
    reservation = payments.reserve(db, operation.id, "customer_wallet", uid, 30)
    claim = mp.OneTimePackageClaim(package_id=1, claim_key="0" * 64, claim_kind="user",
                                   claim_user_id=uid, operation_id=operation.id)
    step = mp.ProvisioningStep(operation_id=operation.id, slot_key="s", direction="create", step_order=0,
                              backend="mikrotik_wg", node_id=1, protocol="wireguard", state="staged")
    db.add_all([claim, step])
    db.commit()
    oid, rid, cid = operation.id, reservation.id, claim.id
    transitions.begin_compensation(db, oid, "node_unavailable")
    db.commit()
    version = operation.version
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))

    # Crash exactly at the final operation UPDATE: the earlier refund and
    # claim release must roll back with it, not survive as partial success.
    original = transitions._write_operation
    def crash(*args, **kwargs):
        raise RuntimeError("injected final write crash")
    transitions._write_operation = crash
    try:
        try:
            compensation.finish(db, oid, version)
            raise AssertionError("crash not injected")
        except RuntimeError:
            db.rollback()
    finally:
        transitions._write_operation = original
    db.expire_all()
    assert db.get(models.User, uid).balance == 70
    assert db.get(mp.PaymentReservation, rid).state == "reserved"
    assert db.get(mp.OneTimePackageClaim, cid).release_seq == 0
    assert db.get(mp.ProvisioningOperation, oid).state == "compensating"
    assert commits == []
    compensation.finish(db, oid, version)
    assert commits == []
    db.commit()
    db.expire_all()
    assert db.get(models.User, uid).balance == 100
    assert db.get(mp.PaymentReservation, rid).state == "released"
    assert db.get(mp.OneTimePackageClaim, cid).release_seq == cid
    assert db.get(mp.ProvisioningOperation, oid).state == "compensated"
    assert db.get(mp.ProvisioningOperation, oid).username_claim is None
    assert db.query(models.LedgerEntry).count() == 0
    compensation.finish(db, oid, version)
    db.commit()
    assert db.get(models.User, uid).balance == 100
    print("PASS", engine.dialect.name, "crash rolls refund/claim/state back; retry refunds exactly once without ledger")

    operation = mp.ProvisioningOperation(operation_type="purchase", business_key=str(uuid.uuid4()),
        request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}",
        target_user_id=uid, state="prepared", wallet_epoch_at_start=0,
        forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
    db.add(operation)
    db.flush()
    reservation = payments.reserve(db, operation.id, "customer_wallet", uid, 20)
    step = mp.ProvisioningStep(operation_id=operation.id, slot_key="s", direction="create", step_order=0,
                              backend="mikrotik_wg", node_id=1, protocol="wireguard", state="staged")
    db.add(step)
    db.commit()
    transitions.begin_remote(db, step.id, 0)
    db.commit()
    transitions.begin_compensation(db, operation.id, "node_unavailable")
    db.commit()
    refusal("provisioning_steps_incomplete", lambda: compensation.finish(db, operation.id, operation.version))
    db.rollback()
    assert reservation.state == "reserved" and db.get(models.User, uid).balance == 80
    refusal("provisioning_operation_changed", lambda: compensation.finish(db, operation.id, operation.version - 1))
    db.rollback()
    operation.approval_uuid = str(uuid.uuid4())
    operation.approval_execution_version = 1
    operation.approval_key_bound = False
    db.commit()
    refusal("provisioning_approval_integration_unavailable",
            lambda: compensation.finish(db, operation.id, operation.version))
    db.rollback()
    assert reservation.state == "reserved" and db.get(models.User, uid).balance == 80
    print("PASS", engine.dialect.name, "unverified remote, stale version and approval integration cannot release money")
    db.close()


with tempfile.TemporaryDirectory(prefix="um-compensation-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
