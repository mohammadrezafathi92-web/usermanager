"""DB-only T1 -> T_final integration, rollback and durable readiness guards."""
import datetime as dt
import os
import sys
import tempfile
import threading
import time
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
from sqlalchemy.exc import OperationalError
from app import models, models_provisioning as mp
from app.services import provisioning_preparation as prep, provisioning_finalization as final
from app.services import provisioning_contracts as contracts, provisioning_schema, receipt_void_schema
from app.services import provisioning_transitions as transitions, resource_leases as locks, wallet_accounts
from app.services.adapter_base import PresentResult
from app.services.provisioning_host import HostIdentity, OwnershipMismatch


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
    db = Factory()
    identity = HostIdentity("a" * 64, str(uuid.uuid4()))
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    installation = runtime.installation_uuid
    # Test fixtures only: no production mode-change or ownership API exists.
    runtime.owner_state = "active"
    runtime.owner_host_id = identity.host_id
    runtime.owner_boot_id = identity.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = dt.datetime.utcnow()
    runtime.ownership_epoch = 1
    runtime.lock_backend = "flock"
    runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.gate_mode = "enforced"
    db.commit()
    arguments = dict(identity=identity, ownership_epoch=1, installation_uuid=installation)

    def run(request, **changes):
        return prep.prepare(db, request, **(arguments | changes))

    for kind in ("create_user", "purchase", "add_connection", "renew_user", "renew_purchase"):
        name = kind + "-" + uuid.uuid4().hex[:8]
        admin = models.AdminUser(username=name, hashed_password="x", balance=100)
        db.add(admin)
        db.flush()
        user = wallet_accounts.create_user_with_wallet(db, username="old-" + name, balance=100,
            telegram_id=12345 + admin.id, total_quota_bytes=10000,
            expire_at=dt.datetime.utcnow() + dt.timedelta(days=10))
        package = models.Package(name="package", quota_gb=1, duration_days=30, price=25,
            max_concurrent_sessions=1)
        node = models.Node(name=name, type=models.NodeType.mikrotik, enabled=True,
            mt_wireguard_interface="wg1", mt_client_subnet="10.1.0.0/24")
        db.add_all([package, node])
        db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=10000,
            expire_at=dt.datetime.utcnow() + dt.timedelta(days=10))
        db.add(purchase)
        db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version(), server_fingerprint="b" * 64,
            config_fingerprint=contracts.config_fingerprint(node), contract='{"not_exist":[]}',
            verified_by_admin_id=admin.id, verified_at=dt.datetime.utcnow()))
        owner = admin.id if kind == "create_user" else None
        request = prep.Preparation(operation_type=kind, business_key=str(uuid.uuid4()),
            tenant_scope_key=wallet_accounts.tenant_scope(db, owner),
            target_user_id=None if kind == "create_user" else user.id,
            intent=final.FinalIntent(user=final.UserSnapshot(username=name if owner else user.username,
                telegram_id=99999 + admin.id if owner else user.telegram_id, owner_admin_id=owner),
                package=None if kind == "add_connection" else final.PackageSnapshot(id=package.id,
                    name=package.name, quota_bytes=1024 ** 3, duration_days=30, max_concurrent_sessions=1),
                sale=None if kind == "add_connection" else final.SaleSnapshot(amount=25,
                    admin_id=owner, payment_method="cash" if owner else "wallet"),
                purchase_id=purchase.id if kind == "renew_purchase" else None,
                quota_bytes=1024 ** 3 if kind.startswith("renew") else 0,
                duration_days=30 if kind.startswith("renew") else 0),
            slots=[] if kind.startswith("renew") else [prep.Slot(slot_key="wg", node_id=node.id,
                protocol="wireguard")] + ([] if kind == "add_connection" else [
                    prep.Slot(slot_key="ppp", node_id=node.id, protocol="pptp")]),
            payers=[] if kind == "add_connection" else [prep.Payer(kind="reseller_credit" if owner else "customer_wallet",
                id=admin.id if owner else user.id, amount=25)])
        db.commit()
        uid, aid, nid = user.id, admin.id, node.id
        db.rollback()
        commits = []
        listener = lambda session: commits.append(True)
        event.listen(db, "after_commit", listener)
        locks.begin_business(db)
        refused("provisioning_type_not_durable", lambda: run(request))
        db.rollback()
        db.query(mp.ProvisioningTypeMode).filter_by(operation_type=kind).update(dict(mode="durable"))
        db.commit()
        commits.clear()
        locks.begin_business(db)
        try:
            run(request, ownership_epoch=2)
            raise AssertionError("wrong host epoch accepted")
        except OwnershipMismatch:
            pass
        db.rollback()
        # Crash after holds/claims/staging (or atomic renewal completion) must
        # undo operation, credential rows, leases, money and every benefit.
        before = (db.query(mp.ProvisioningOperation).count(), db.query(mp.ProvisioningStep).count(),
            db.query(mp.PaymentReservation).count(), db.query(models.Connection).count(),
            db.query(models.LedgerEntry).count())
        db.rollback()
        # Mode/host readiness cannot be bypassed even for local renewals.
        for values, detail in [(dict(gate_mode="shadow"), "gate_not_enforced"),
                (dict(owner_state="draining"), "provisioning_draining")]:
            locks.begin_business(db)
            db.query(mp.ProvisioningRuntimeState).filter_by(id=1).update(values)
            db.flush()
            refused(detail, lambda: run(request))
            db.rollback()
        if request.payers:
            invalid = request.copy(update={"intent": request.intent.copy(update={"sale": None})})
            locks.begin_business(db)
            refused("provisioning_payment_invalid", lambda: run(invalid))
            db.rollback()
            reserve = prep.payment_reservations.reserve

            def crash_after_hold(*args, **kwargs):
                reserve(*args, **kwargs)
                raise HTTPException(409, "injected_hold_crash")

            locks.begin_business(db)
            with patch.object(prep.payment_reservations, "reserve", side_effect=crash_after_hold):
                refused("injected_hold_crash", lambda: run(request))
            assert not commits
            db.rollback()
            assert before[:3] == (db.query(mp.ProvisioningOperation).count(),
                db.query(mp.ProvisioningStep).count(), db.query(mp.PaymentReservation).count())
            assert db.get(models.User, uid).balance == db.get(models.AdminUser, aid).balance == 100
            db.rollback()
            db.expunge_all()
        if request.slots:
            blocker = locks.acquire(db, f"node:{nid}:wg_pool", "op:999999:1")
            db.commit()
            commits.clear()
            locks.begin_business(db)
            try:
                run(request)
                raise AssertionError("busy lease accepted")
            except locks.LeaseBusy:
                pass
            db.rollback()
            assert before[0] == db.query(mp.ProvisioningOperation).count()
            db.rollback()
            locks.release(db, blocker)
            db.commit()
            commits.clear()
        locks.begin_business(db)
        result = run(request)
        assert not commits
        tokens = result.leases
        assert result.operation.state == ("completed" if kind.startswith("renew") else "prepared")
        oid = result.operation.id
        if not kind.startswith("renew"):
            rows = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).order_by(mp.ProvisioningStep.step_order).all()
            assert rows[0].staged_wg_private_key and rows[0].wg_public_key and rows[0].wg_client_address
            assert all(not row.remote_attempted for row in rows)
            assert db.query(models.Connection).count() == before[3]
        db.rollback()
        assert before == (db.query(mp.ProvisioningOperation).count(), db.query(mp.ProvisioningStep).count(),
            db.query(mp.PaymentReservation).count(), db.query(models.Connection).count(), db.query(models.LedgerEntry).count())
        assert db.get(models.User, uid).balance == db.get(models.AdminUser, aid).balance == 100
        db.rollback()
        db.expunge_all()  # Discard rolled-back staged objects before SQLite reuses IDs.
        locks.begin_business(db)
        result = run(request)
        oid, tokens = result.operation.id, result.leases
        db.commit()
        assert len(commits) == 1
        db.rollback()
        locks.begin_business(db)
        replay = run(request)
        assert replay.replay and replay.operation.id == oid and not replay.leases
        db.rollback()
        changed = request.copy(update={"tenant_scope_key": "changed"})
        locks.begin_business(db)
        refused("provisioning_request_changed", lambda: run(changed))
        db.rollback()
        if not kind.startswith("renew"):
            steps = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).all()
            ids = [row.id for row in steps if row.backend != "radius_ppp"]
            db.rollback()
            # Simulated adapter responses, never actual router calls.
            for sid in ids:
                locks.begin_business(db)
                locks.revalidate(db, tokens)
                row = db.get(mp.ProvisioningStep, sid)
                row = transitions.begin_remote(db, sid, row.version)
                db.commit()
                locks.begin_business(db)
                locks.revalidate(db, tokens)
                row = db.get(mp.ProvisioningStep, sid)
                transitions.confirm_present(db, sid, row.version, PresentResult(created=True))
                db.commit()
            locks.begin_business(db)
            locks.revalidate(db, tokens)
            transitions.mark_remote_complete(db, oid)
            db.commit()
            # Endpoint drift cannot deliver a resource against a different node.
            db.query(models.Node).filter_by(id=nid).update(dict(mt_host="changed-endpoint"))
            db.commit()
            locks.begin_business(db)
            operation = db.get(mp.ProvisioningOperation, oid)
            refused("provisioning_node_config_changed", lambda: final.finish(db, oid, operation.version, tokens))
            db.rollback()
            db.query(models.Node).filter_by(id=nid).update(dict(mt_host=None))
            db.commit()
            locks.begin_business(db)
            operation = db.get(mp.ProvisioningOperation, oid)
            final.finish(db, oid, operation.version, tokens)
            db.commit()
        operation = db.get(mp.ProvisioningOperation, oid)
        assert operation.state == "completed"
        assert all(row.state == "captured" for row in db.query(mp.PaymentReservation).filter_by(operation_id=oid))
        assert db.get(models.User, uid).balance == (75 if kind in ("purchase", "renew_user", "renew_purchase") else 100)
        assert db.get(models.AdminUser, aid).balance == (75 if owner else 100)
        assert db.query(models.LedgerEntry).count() > before[4] or kind == "add_connection"
        db.rollback()
        for token in tokens:
            locks.release(db, token)
        db.commit()
        event.remove(db, "after_commit", listener)
        print("PASS", engine.dialect.name, kind, "atomic preparation, stable replay, completion and rollback")
    # All nine backend identities are generated and committed BEFORE a mock
    # adapter can see them. No adapter/client is invoked by preparation.
    for backend, mode, protocol in [("mikrotik_wg", None, "wireguard"), ("radius_ppp", None, "pptp"),
            ("softether", None, "softether"), ("xray_ssh", "ssh", "xray"), ("threexui", "3xui", "xray"),
            ("marzban", "marzban", "xray"), ("hiddify", "hiddify", "xray"),
            ("marzneshin", "marzneshin", "xray"), ("sui", "sui", "xray")]:
        node = models.Node(name=str(uuid.uuid4()), enabled=True, xr_panel_mode=mode,
            type=models.NodeType.softether if backend == "softether" else models.NodeType.xray if mode else models.NodeType.mikrotik,
            mt_wireguard_interface="wg1", mt_client_subnet="10.2.0.0/24")
        db.add(node)
        db.flush()
        nid = node.id
        user = db.get(models.User, uid)
        request = prep.Preparation(operation_type="add_connection", business_key=str(uuid.uuid4()),
            tenant_scope_key=wallet_accounts.tenant_scope(db, None), target_user_id=uid,
            intent=final.FinalIntent(user=final.UserSnapshot(username=user.username, telegram_id=user.telegram_id)),
            slots=[prep.Slot(slot_key="new", node_id=nid, protocol=protocol)])
        db.commit()
        if backend != "radius_ppp":
            locks.begin_business(db)
            refused("node_contract_not_ready", lambda: run(request))
            db.rollback()
            assert not db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count()
            db.add(mp.ProvisioningNodeContract(node_id=nid, backend=backend, state="ready",
                adapter_version=contracts.adapter_version(), server_fingerprint="c" * 64,
                config_fingerprint=contracts.config_fingerprint(db.get(models.Node, nid)),
                contract='{"not_exist":[]}', verified_by_admin_id=aid, verified_at=dt.datetime.utcnow()))
            db.commit()
            for values in (dict(adapter_version="0" * 40), dict(config_fingerprint="0" * 64),
                    dict(contract='{"not_exist":[{"kind":"http","op":"read","status":404}]}'),
                    dict(contract='{"not_exist":[{"kind":"http","op":"read","status":404,"json_path":"anything","equals":"gone"}]}')):
                locks.begin_business(db)
                db.query(mp.ProvisioningNodeContract).filter_by(node_id=nid, backend=backend).update(values)
                db.flush()
                refused("node_contract_not_ready", lambda: run(request))
                db.rollback()
        db.rollback()
        locks.begin_business(db)
        result = run(request)
        oid = result.operation.id
        row = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
        assert row.backend == backend and row.state == "staged" and not row.remote_attempted
        assert row.staged_wg_private_key if backend == "mikrotik_wg" else (
            row.staged_password if backend in ("radius_ppp", "softether") else row.staged_xr_uuid)
        saved = (row.xr_email, row.staged_xr_uuid, row.account_username, row.staged_password, row.staged_wg_private_key)
        tokens = result.leases
        db.commit()
        locks.begin_business(db)
        replay = run(request)
        row = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
        assert replay.replay and saved == (row.xr_email, row.staged_xr_uuid,
            row.account_username, row.staged_password, row.staged_wg_private_key)
        db.rollback()
        for token in tokens:
            locks.release(db, token)
        db.commit()
        print("PASS", engine.dialect.name, backend, "stable pre-remote identity and readiness validation")
    # Two real sessions submit the same business request. Deadlock victims
    # retry the WHOLE transaction; never retry only a hold or INSERT.
    node = models.Node(name="race-" + uuid.uuid4().hex, enabled=True, type=models.NodeType.mikrotik)
    db.add(node)
    db.flush()
    user = db.get(models.User, uid)
    request = prep.Preparation(operation_type="add_connection", business_key=str(uuid.uuid4()),
        tenant_scope_key="shared", target_user_id=uid,
        intent=final.FinalIntent(user=final.UserSnapshot(username=user.username, telegram_id=user.telegram_id)),
        slots=[prep.Slot(slot_key="race", node_id=node.id, protocol="pptp")])
    db.commit()
    barrier, results, errors = threading.Barrier(2), [], []

    def worker():
        session = Factory()
        try:
            barrier.wait(timeout=5)
            for attempt in range(6):
                try:
                    locks.begin_business(session)
                    result = prep.prepare(session, request, **arguments)
                    oid, replay, leases = result.operation.id, result.replay, result.leases
                    session.commit()
                    results.append((oid, replay, leases))
                    break
                except (OperationalError, locks.LeaseBusy) as exc:
                    session.rollback()
                    if isinstance(exc, OperationalError) and engine.dialect.name != "sqlite" and (
                            not exc.orig.args or exc.orig.args[0] not in (1020, 1205, 1213)):
                        raise
                    if attempt == 5:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        except Exception as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert all(not thread.is_alive() for thread in threads) and not errors, errors
    assert len(results) == 2 and results[0][0] == results[1][0] and sorted(row[1] for row in results) == [False, True]
    oid = results[0][0]
    assert db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count() == 1
    assert db.query(mp.ProvisioningStep).filter_by(operation_id=oid).count() == 1
    db.rollback()
    for _, _, tokens in results:
        for token in tokens:
            locks.release(db, token)
    db.commit()
    print("PASS", engine.dialect.name, "two real sessions: exactly one T1 and stable replay")
    db.close()


with tempfile.TemporaryDirectory(prefix="um-preparation-") as directory:
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
