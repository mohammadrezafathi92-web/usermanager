"""Prepared operation coordinator, fake nodes, disposable SQLite/MariaDB only."""
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
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from fastapi import HTTPException
from app import models, models_provisioning as mp
from app.services import provisioning_schema, receipt_void_schema, provisioning_contracts as contracts
from app.services import provisioning_preparation as prep, provisioning_finalization as final
from app.services import provisioning_operation_worker as worker, provisioning_parent_execute as execute
from app.services import resource_leases, wallet_accounts, remote_action as action
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        installation = runtime.installation_uuid
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
        runtime.ownership_epoch, runtime.gate_mode = 1, "enforced"  # Owned test fixture, never a live control API.
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        admin = models.AdminUser(username="owner", hashed_password="test", balance=100)
        user = wallet_accounts.create_user_with_wallet(db, username="payer", balance=100, telegram_id=42)
        package = models.Package(name="package", quota_gb=1, duration_days=30, price=25)
        node = models.Node(name="fake-node", type=models.NodeType.mikrotik, enabled=True,
            mt_wireguard_interface="wg0", mt_client_subnet="10.1.0.0/24")
        db.add_all([admin, package, node])
        db.flush()
        uid, aid, pid, nid = user.id, admin.id, package.id, node.id
        db.add(mp.ProvisioningNodeContract(node_id=nid, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=aid,
            verified_at=dt.datetime.utcnow()))
        for kind in worker.KINDS:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
    expected = dict(installation_uuid=installation, ownership_epoch=1)

    def prepared(kind="purchase"):
        with Factory() as db:
            request = prep.Preparation(operation_type=kind, business_key=str(uuid.uuid4()),
                tenant_scope_key=wallet_accounts.tenant_scope(db, aid if kind == "create_user" else None),
                target_user_id=None if kind == "create_user" else uid,
                intent=final.FinalIntent(user=final.UserSnapshot(username="new-" + uuid.uuid4().hex[:8] if kind == "create_user" else "payer",
                    telegram_id=1000 + pid if kind == "create_user" else 42, owner_admin_id=aid if kind == "create_user" else None),
                    package=None if kind == "add_connection" else final.PackageSnapshot(id=pid, name="package", quota_bytes=1024**3,
                        duration_days=30), sale=None if kind == "add_connection" else final.SaleSnapshot(amount=25,
                        admin_id=aid if kind == "create_user" else None, payment_method="cash" if kind == "create_user" else "wallet")),
                slots=[prep.Slot(slot_key="wg", node_id=nid, protocol="wireguard")],
                payers=[] if kind == "add_connection" else [prep.Payer(kind="reseller_credit" if kind == "create_user" else "customer_wallet",
                    id=aid if kind == "create_user" else uid, amount=25)])
            db.rollback()
            resource_leases.begin_business(db)
            result = prep.prepare(db, request, identity=host, **expected)
            oid, tokens = result.operation.id, result.leases
            db.commit()
            return oid, tokens

    def tick(oid, tokens, **extra):
        return worker.tick(Factory, oid, host, tokens, **(expected | extra))

    def release(tokens):
        with Factory() as db:
            for token in tokens:
                resource_leases.release(db, token)
            db.commit()

    seen = []
    def remote(dto):
        assert engine.pool.checkedout() == 0, "worker kept a parent DB connection over remote I/O"
        seen.append(dto.action_type)
        return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)

    with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}), (
            patch.object(execute.remote_runner, "run_action", side_effect=remote)) as launch:
        for kind in worker.KINDS:
            oid, tokens = prepared(kind)
            try:
                tick(oid, tokens, ownership_epoch=2)
                raise AssertionError("stale ownership accepted")
            except HTTPException as error:
                assert error.detail == "provisioning_worker_owner_changed"
            before_calls = launch.call_count
            try:
                tick(oid, tuple(token for token in tokens if not token.resource_key.startswith("node:")))
                raise AssertionError("incomplete lease scope accepted")
            except HTTPException as error:
                assert error.detail == "provisioning_lease_scope_invalid"
            assert launch.call_count == before_calls
            assert tick(oid, tokens)["step"]["state"] == "remote_created"
            if kind == "purchase":
                def crash(db):
                    if db.get(mp.ProvisioningOperation, oid).state == "completed":
                        raise RuntimeError("injected final commit crash")
                event.listen(Factory.class_, "before_commit", crash)
                try:
                    try:
                        tick(oid, tokens)
                        raise AssertionError("commit crash missed")
                    except RuntimeError:
                        pass
                finally:
                    event.remove(Factory.class_, "before_commit", crash)
                with Factory() as db:
                    assert db.get(mp.ProvisioningOperation, oid).state == "provisioning"
                    assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
            assert tick(oid, tokens)["state"] == "completed"
            count = launch.call_count
            assert tick(oid, tokens)["status"] == "terminal" and launch.call_count == count
            release(tokens)
        # Ambiguous write -> waiting (no delivery/capture), then deadline
        # compensation -> verified cleanup -> ONE release, on a later tick.
        oid, tokens = prepared()
        with Factory() as db:
            held_balance = db.get(models.User, uid).balance
        with patch.object(execute.remote_runner, "run_action", side_effect=lambda dto:
                action.RemoteActionResult.killed_unknown(dto.action_id, "hard_deadline_exceeded")):
            assert tick(oid, tokens)["step"]["state"] == "remote_calling"
            assert tick(oid, tokens)["status"] == "waiting"
        with Factory() as db:
            operation = db.get(mp.ProvisioningOperation, oid)
            operation.forward_deadline = dt.datetime(2000, 1, 1)
            db.commit()
        assert tick(oid, tokens)["status"] == "compensation_started"
        assert tick(oid, tokens)["status"] == "waiting"
        with Factory() as db:
            # Advance ONLY the owned scratch retry fixture, never a live job.
            db.query(mp.ProvisioningStep).filter_by(operation_id=oid).update(dict(next_retry_at=None))
            db.commit()
        def cleanup(dto):
            assert engine.pool.checkedout() == 0
            assert dto.action_type is action.ActionType.WG_ENSURE_ABSENT
            assert dto.fencing["binding"]["phase"] == "compensation"
            return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED,
                write_attempted=True, remote_outcome="verified_absent")
        with patch.object(execute.remote_runner, "run_action", side_effect=cleanup):
            assert tick(oid, tokens)["step"]["state"] == "removed"
        with Factory() as db:
            assert db.get(models.User, uid).balance == held_balance
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
        assert tick(oid, tokens)["state"] == "compensated"
        assert tick(oid, tokens)["status"] == "terminal"
        with Factory() as db:
            assert db.get(models.User, uid).balance == held_balance + 25
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "released"
        release(tokens)
    assert _no_network.attempts == []
    print("PASS", engine.dialect.name, "three operation kinds, closed sessions, atomic completion, unknown wait and once-only compensation")


with tempfile.TemporaryDirectory(prefix="um-worker-") as directory:
    local = create_engine("sqlite:///" + directory + "/worker.db")
    try:
        scenario(local)
    finally:
        local.dispose()
url = os.environ.get("MARIADB_TEST_URL")
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
