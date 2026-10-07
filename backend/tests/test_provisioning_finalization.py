"""Whole T_final: five types, financial/secret rollback and once-only retry."""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_finalization as final, provisioning_schema, receipt_void_schema
from app.services import payment_reservations as payments, resource_leases as locks, wallet_accounts


def refused(code, callback):
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
    for kind in ("create_user", "purchase", "add_connection", "renew_user", "renew_purchase"):
        db = Factory()
        name = kind + "-" + uuid.uuid4().hex[:8]
        admin = models.AdminUser(username=name, hashed_password="x", balance=100)
        db.add(admin)
        db.flush()
        owner = admin.id if kind == "create_user" else None
        user = wallet_accounts.create_user_with_wallet(db, username="old-" + name, balance=100,
            telegram_id=12345 + admin.id, total_quota_bytes=10000, expire_at=dt.datetime.utcnow() + dt.timedelta(days=10))
        package = models.Package(name="frozen package", quota_gb=1, duration_days=30, price=25)
        node = models.Node(name=name, type=models.NodeType.mikrotik, enabled=True, mt_wireguard_interface="wg1")
        db.add_all([package, node])
        db.flush()
        old_purchase = models.Purchase(user_id=user.id, package_id=package.id,
            quota_bytes=10000, expire_at=dt.datetime.utcnow() + dt.timedelta(days=10))
        db.add(old_purchase)
        db.flush()
        intent = final.FinalIntent(user=final.UserSnapshot(username=name if kind == "create_user" else user.username,
            telegram_id=99999 + admin.id if kind == "create_user" else user.telegram_id, owner_admin_id=owner),
            package=None if kind == "add_connection" else final.PackageSnapshot(id=package.id, name="frozen package",
                quota_bytes=1024 ** 3, duration_days=30, max_concurrent_sessions=1),
            sale=None if kind == "add_connection" else final.SaleSnapshot(amount=25,
                admin_id=owner, payment_method="cash" if kind == "create_user" else "wallet"),
            purchase_id=old_purchase.id if kind == "renew_purchase" else None,
            quota_bytes=1024 ** 3 if kind.startswith("renew") else 0,
            duration_days=30 if kind.startswith("renew") else 0)
        operation = mp.ProvisioningOperation(operation_type=kind, business_key=str(uuid.uuid4()), request_hash="0" * 64,
            tenant_scope_key=wallet_accounts.tenant_scope(db, owner), actor_kind="system", intent=intent.json(),
            target_user_id=None if kind == "create_user" else user.id,
            username_claim=name if kind == "create_user" else None, state="prepared", wallet_epoch_at_start=0,
            forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
        db.add(operation)
        db.flush()
        steps = []
        if not kind.startswith("renew"):
            protocols = ("wireguard",) if kind == "add_connection" else ("wireguard", "pptp")
            for order, protocol in enumerate(protocols):
                step = mp.ProvisioningStep(operation_id=operation.id, slot_key=f"request_slot:{order}", step_order=order,
                    direction="create", node_id=node.id, backend="mikrotik_wg" if protocol == "wireguard" else "radius_ppp",
                    protocol=protocol, state="remote_created" if protocol == "wireguard" else "staged",
                    remote_attempted=protocol == "wireguard", wg_interface="wg1", wg_peer_name=name,
                    wg_public_key="public", wg_client_address="10.0.0.2/32", staged_wg_private_key="PRIVATE_SECRET",
                    account_username=name, staged_password="PASSWORD_SECRET")
                db.add(step)
                db.flush()
                steps.append(step.id)
        if kind != "add_connection":
            payments.reserve(db, operation.id, "reseller_credit" if kind == "create_user" else "customer_wallet",
                admin.id if kind == "create_user" else user.id, 25)
        if not kind.startswith("renew"):
            operation.state = "remote_complete"
        db.commit()
        oid, uid, aid, pid = operation.id, user.id, admin.id, old_purchase.id
        identity = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(rv.wallet_accounts.c.user_id == uid)).scalar_one()
        db.rollback()
        tokens = [locks.acquire(db, f"provisioning_op:{oid}", f"op:{oid}:1")]
        if kind != "create_user":
            tokens.append(locks.acquire(db, f"customer_identity:{identity}", f"op:{oid}:1"))
        db.commit()
        commits = []
        event.listen(db, "after_commit", lambda session: commits.append(True))
        counts = (db.query(models.User).count(), db.query(models.Purchase).count(),
                  db.query(models.Connection).count(), db.query(models.LedgerEntry).count())
        db.rollback()
        if kind != "create_user":
            locks.begin_business(db)
            refused("provisioning_lease_scope_invalid", lambda: final.finish(db, oid, 0, tokens[:1]))
            db.rollback()
            locks.begin_business(db)
            refused("provisioning_lease_scope_invalid", lambda: final.finish(db, oid, 0, tokens[1:]))
            db.rollback()
        locks.begin_business(db)
        refused("provisioning_operation_changed", lambda: final.finish(db, oid, 1, tokens))
        db.rollback()
        if kind != "add_connection":
            locks.begin_business(db)
            db.query(mp.ProvisioningOperation).filter_by(id=oid).update(dict(approval_uuid=str(uuid.uuid4()),
                approval_execution_version=0, approval_key_bound=False))
            refused("provisioning_approval_integration_unavailable", lambda: final.finish(db, oid, 0, tokens))
            db.rollback()
        # A failure at the very last operation CAS must undo every preceding
        # resource creation, benefit, ledger entry, secret clear and capture.
        locks.begin_business(db)
        with patch.object(final.transitions, "_write_operation", side_effect=HTTPException(409, "injected_final_cas")):
            refused("injected_final_cas", lambda: final.finish(db, oid, 0, tokens))
        assert not commits
        db.rollback()
        assert counts == (db.query(models.User).count(), db.query(models.Purchase).count(),
                          db.query(models.Connection).count(), db.query(models.LedgerEntry).count())
        assert db.get(mp.ProvisioningOperation, oid).state != "completed"
        for sid in steps:
            assert db.get(mp.ProvisioningStep, sid).staged_wg_private_key == "PRIVATE_SECRET"
        reservations = db.query(mp.PaymentReservation).filter_by(operation_id=oid).all()
        assert all(row.state == "reserved" for row in reservations)
        assert db.get(models.User, uid).balance == (75 if kind in ("purchase", "renew_user", "renew_purchase") else 100)
        assert db.get(models.AdminUser, aid).balance == (75 if kind == "create_user" else 100)
        db.rollback()
        # Live package edits must not change the already-frozen sale or limits.
        db.query(models.Package).filter_by(id=package.id).update(dict(name="edited", quota_gb=999, duration_days=99, price=999))
        db.commit()
        commits.clear()
        locks.begin_business(db)
        result = final.finish(db, oid, 0, tokens)
        assert result.state == "completed" and not commits
        result_uid, result_pid, ledger_id = result.result_user_id, result.result_purchase_id, result.sale_ledger_entry_id
        if kind in ("create_user", "purchase"):
            purchase = db.get(models.Purchase, result_pid)
            assert purchase.quota_bytes == 1024 ** 3 and purchase.package_name_snapshot == "frozen package"
        if kind == "renew_user":
            assert db.get(models.User, uid).reserved_quota_bytes == 1024 ** 3
        if kind == "renew_purchase":
            assert db.get(models.Purchase, pid).reserved_quota_bytes == 1024 ** 3
        assert all(db.get(mp.ProvisioningStep, sid).state == "active" and
                   db.get(mp.ProvisioningStep, sid).staged_wg_private_key is None for sid in steps)
        if ledger_id:
            assert db.get(models.LedgerEntry, ledger_id).amount == 25
        assert all(row.state == "captured" for row in reservations)
        db.commit()
        assert len(commits) == 1
        after = (db.query(models.User).count(), db.query(models.Purchase).count(),
                 db.query(models.Connection).count(), db.query(models.LedgerEntry).count())
        db.rollback()
        locks.begin_business(db)
        replay = final.finish(db, oid, 0, tokens)
        assert (replay.result_user_id, replay.result_purchase_id, replay.sale_ledger_entry_id) == (result_uid, result_pid, ledger_id)
        db.commit()
        assert after == (db.query(models.User).count(), db.query(models.Purchase).count(),
                         db.query(models.Connection).count(), db.query(models.LedgerEntry).count())
        db.rollback()
        for token in tokens:
            locks.release(db, token)
        db.commit()
        db.close()
        print("PASS", engine.dialect.name, kind, "whole transaction rollback, frozen projection and once-only completion")


with tempfile.TemporaryDirectory(prefix="um-finalization-") as directory:
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
