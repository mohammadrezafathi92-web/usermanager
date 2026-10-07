"""Pre-cutover payment holds: no double debit, no unverified refund.

Runs only on isolated SQLite / disposable real MariaDB in CI. No nodes.
"""
import datetime as dt
import os
import sys
import tempfile
import threading
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.routers import admins as admins_router
from app.services import payment_reservations as payments
from app.services import provisioning_schema, receipt_void_schema, wallet_accounts, wallet_identity, wallet_service


def refused(code, callback):
    try:
        callback()
        raise AssertionError("expected refusal: " + code)
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine):
    label = engine.dialect.name
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    user = wallet_accounts.create_user_with_wallet(db, username="payer", telegram_id=123, balance=100)
    admin = models.AdminUser(username="seller", hashed_password="x", balance=100, credit_limit=50)
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    db.add_all([admin, root])
    db.commit()
    uid, aid, root_id = user.id, admin.id, root.id
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))

    def operation(state="prepared"):
        row = mp.ProvisioningOperation(operation_type="purchase", business_key=str(uuid.uuid4()), request_hash="0" * 64,
            tenant_scope_key="shared", actor_kind="system", intent="{}", target_user_id=uid,
            state=state, forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5), wallet_epoch_at_start=0)
        db.add(row)
        db.flush()
        return row

    def balance(model, ident):
        db.expire_all()
        return db.get(model, ident).balance

    op = operation()
    row = payments.reserve(db, op.id, "customer_wallet", uid, 30)
    rid, oid = row.id, op.id
    assert commits == [] and balance(models.User, uid) == 70
    assert payments.reserve(db, oid, "customer_wallet", uid, 30).id == rid
    refused("payment_reservation_conflict", lambda: payments.reserve(db, oid, "customer_wallet", uid, 31))
    assert balance(models.User, uid) == 70
    db.rollback()
    assert balance(models.User, uid) == 100 and db.query(mp.PaymentReservation).count() == 0
    print("PASS", label, "reserve/retry/rollback are atomic and no function commits")

    op = operation()
    row = payments.reserve(db, op.id, "customer_wallet", uid, 30)
    rid, oid = row.id, op.id
    db.commit()
    refused("wallet_account_has_hold", lambda: wallet_accounts.prepare_user_deletion(db, db.get(models.User, uid)))
    refused("wallet_account_has_hold", lambda: wallet_identity.change(db, db.get(models.User, uid), telegram_id=456))
    with patch.object(provisioning_schema, "is_ready", return_value=False):
        refused("wallet_account_has_hold", lambda: wallet_accounts.prepare_user_deletion(db, db.get(models.User, uid)))
        refused("payment_reservation_schema_unavailable", lambda: payments.reserve(db, oid, "customer_wallet", uid, 30))
    db.rollback()
    print("PASS", label, "held payer cannot be deleted or rebound, including degraded provisioning readiness")

    op = db.get(mp.ProvisioningOperation, oid)
    op.state = "compensating"
    step = mp.ProvisioningStep(operation_id=oid, slot_key="slot1", direction="create", step_order=0, backend="mikrotik_wg", node_id=1,
        protocol="wireguard", state="cleanup_required", remote_attempted=True, remote_outcome="unverified")
    db.add(step)
    db.flush()
    refused("payment_reservation_remote_unverified", lambda: payments.release(db, rid))
    assert balance(models.User, uid) == 70
    step = db.get(mp.ProvisioningStep, step.id)
    step.state, step.remote_outcome = "removed", "verified_absent"
    db.flush()
    assert payments.release(db, rid).state == "released"
    assert balance(models.User, uid) == 100 and db.query(models.LedgerEntry).count() == 0
    db.rollback()
    assert balance(models.User, uid) == 70 and db.get(mp.PaymentReservation, rid).state == "reserved"
    op = db.get(mp.ProvisioningOperation, oid)
    op.state = "compensating"
    db.flush()
    payments.release(db, rid)
    db.commit()
    payments.release(db, rid)
    db.commit()
    assert balance(models.User, uid) == 100
    refused("payment_reservation_already_released", lambda: payments.capture(db, rid, sale_ledger_entry_id=1))
    db.rollback()
    print("PASS", label, "release requires verified remote absence, rolls back and retries without a cash ledger")

    op = operation()
    row = payments.reserve(db, op.id, "customer_wallet", uid, 40)
    rid, oid = row.id, op.id
    db.commit()
    op = db.get(mp.ProvisioningOperation, oid)
    op.state = "remote_complete"
    entry = models.LedgerEntry(kind="sale_new", amount=40, user_id=uid)
    wrong = models.LedgerEntry(kind="wallet_topup", amount=40, user_id=uid)
    db.add_all([entry, wrong])
    db.flush()
    refused("payment_reservation_ledger_mismatch", lambda: payments.capture(db, rid, sale_ledger_entry_id=wrong.id))
    entry_id = entry.id
    payments.capture(db, rid, sale_ledger_entry_id=entry_id)
    db.commit()
    payments.capture(db, rid, sale_ledger_entry_id=entry_id)
    db.commit()
    assert balance(models.User, uid) == 60 and db.get(mp.PaymentReservation, rid).state == "captured"
    refused("payment_reservation_already_captured", lambda: payments.release(db, rid))
    db.rollback()
    print("PASS", label, "customer capture binds the actual sale ledger, never debits twice or releases a captured sale")

    op = operation()
    row = payments.reserve(db, op.id, "reseller_credit", aid, 80)
    rid, oid = row.id, op.id
    assert balance(models.AdminUser, aid) == 20
    assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_spend").count() == 0
    db.commit()
    refused("reseller_credit_has_hold", lambda: admins_router.delete_admin(
        aid, db=db, current=db.get(models.AdminUser, root_id), _confirm=None))
    db.rollback()
    op = db.get(mp.ProvisioningOperation, oid)
    op.state = "remote_complete"
    db.flush()
    payments.capture(db, rid)
    assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_spend").count() == 1
    db.rollback()
    assert db.get(mp.PaymentReservation, rid).state == "reserved"
    assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_spend").count() == 0
    db.get(mp.ProvisioningOperation, oid).state = "remote_complete"
    db.flush()
    payments.capture(db, rid)
    db.commit()
    payments.capture(db, rid)
    db.commit()
    assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_spend").count() == 1
    assert balance(models.AdminUser, aid) == 20
    print("PASS", label, "reseller reserve has no ledger; capture/rollback/retry create exactly one spend")

    op = operation()
    row = payments.reserve(db, op.id, "reseller_credit", aid, 50)
    rid, oid = row.id, op.id
    assert balance(models.AdminUser, aid) == -30
    db.commit()
    db.get(mp.ProvisioningOperation, oid).state = "compensating"
    db.flush()
    payments.release(db, rid)
    db.commit()
    payments.release(db, rid)
    db.commit()
    assert balance(models.AdminUser, aid) == 20
    assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_spend").count() == 1
    op = operation()
    try:
        payments.reserve(db, op.id, "reseller_credit", aid, 71)
        raise AssertionError("overdraft floor ignored")
    except HTTPException as exc:
        assert exc.status_code == 400
    db.rollback()
    assert balance(models.AdminUser, aid) == 20
    print("PASS", label, "reseller release is not a refund ledger and overdraft floor remains enforced")
    op = operation()
    oid = op.id
    db.commit()
    db.close()
    barrier = threading.Barrier(2)
    returned, errors = [], []

    def contender():
        session = Factory()
        try:
            barrier.wait(timeout=10)
            for attempt in range(3):
                try:
                    if engine.dialect.name == "sqlite":
                        session.execute(text("BEGIN IMMEDIATE"))
                    result = payments.reserve(session, oid, "customer_wallet", uid, 10)
                    rid = result.id
                    session.commit()
                    returned.append(rid)
                    break
                except OperationalError:
                    session.rollback()
                    if attempt == 2:
                        raise
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            session.close()

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and not any(t.is_alive() for t in threads), errors
    verify = Factory()
    try:
        assert len(returned) == 2 and len(set(returned)) == 1
        assert verify.get(models.User, uid).balance == 50
        assert verify.query(mp.PaymentReservation).filter_by(operation_id=oid).count() == 1
    finally:
        verify.close()
    print("PASS", label, "two real concurrent sessions reserve once for the same operation")


with tempfile.TemporaryDirectory(prefix="um-reservations-") as directory:
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
    raise AssertionError("real MariaDB is mandatory in CI")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
