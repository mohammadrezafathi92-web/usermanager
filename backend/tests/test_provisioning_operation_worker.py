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
from app import models, models_provisioning as mp, models_receipt_void as rv
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
        user = wallet_accounts.create_user_with_wallet(db, username="payer", balance=250, telegram_id=42)
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

    def request_for(kind="purchase"):
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
            return request

    def prepared(kind="purchase"):
        request = request_for(kind)
        with Factory() as db:
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
        assert dto.credential["wg_private_key"] is None, "customer private key sent to remote child"
        seen.append(dto.action_type)
        return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)

    with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}), (
            patch.object(execute.remote_runner, "run_action", side_effect=remote)) as launch:
        # Crash after committed T1: acquire-all is atomic, never steals a
        # live node lease, and resumes the SAME operation / hold / identity.
        oid, old_tokens = prepared()
        with Factory() as db:
            staged = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
            saved_identity = (staged.id, staged.wg_public_key, staged.staged_wg_private_key)
            hold_id = db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().id
            for token in old_tokens:
                if not token.resource_key.startswith("node:"):
                    resource_leases.release(db, token)
            db.commit()
        try:
            worker.reacquire(Factory, oid, host, **expected)
            raise AssertionError("live lease stolen")
        except resource_leases.LeaseBusy:
            pass
        with Factory() as db:
            for token in old_tokens:
                row = db.execute(rv.resource_locks.select().where(
                    rv.resource_locks.c.resource_key == token.resource_key)).mappings().one()
                if token.resource_key.startswith("node:"):
                    assert row["lease_owner"] == token.owner
                else:
                    assert row["lease_owner"] is None, "partial recovery acquisition committed"
            db.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key.in_(
                [token.resource_key for token in old_tokens])).values(leased_until=dt.datetime(2000, 1, 1)))
            db.commit()
        recovered = worker.reacquire(Factory, oid, host, **expected)
        assert {token.resource_key for token in recovered} == {token.resource_key for token in old_tokens}
        assert all(new.epoch > old.epoch and new.owner != old.owner
            for new, old in zip(sorted(recovered, key=lambda t: t.resource_key),
                                sorted(old_tokens, key=lambda t: t.resource_key)))
        with Factory() as db:
            resource_leases.begin_business(db)
            try:
                resource_leases.revalidate(db, old_tokens)
                raise AssertionError("stale executor accepted")
            except resource_leases.LeaseLost:
                db.rollback()
            assert all(not resource_leases.release(db, token) for token in old_tokens)
            db.commit()
            staged = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one()
            assert (staged.id, staged.wg_public_key, staged.staged_wg_private_key) == saved_identity
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().id == hold_id
        assert tick(oid, recovered)["step"]["state"] == "remote_created"
        assert tick(oid, recovered)["state"] == "completed"
        assert worker.reacquire(Factory, oid, host, **expected) == ()
        release(recovered)
        # The bounded entry owns its lease lifecycle, without a live worker.
        oid, initial = prepared()
        count = launch.call_count
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "lease_busy"
        assert launch.call_count == count
        release(initial)  # Simulated graceful loss of the original executor.
        assert worker.resume_one(Factory, oid, host, **expected)["step"]["state"] == "remote_created"
        with Factory() as db:
            assert all(row["lease_owner"] is None for row in db.execute(rv.resource_locks.select().where(
                rv.resource_locks.c.resource_key.in_([token.resource_key for token in initial]))).mappings())
        def final_crash(db):
            if db.get(mp.ProvisioningOperation, oid).state == "completed":
                raise RuntimeError("resume final commit crash")
        event.listen(Factory.class_, "before_commit", final_crash)
        try:
            try:
                worker.resume_one(Factory, oid, host, **expected)
                raise AssertionError("resume commit crash missed")
            except RuntimeError:
                pass
        finally:
            event.remove(Factory.class_, "before_commit", final_crash)
        with Factory() as db:
            assert db.get(mp.ProvisioningOperation, oid).state == "provisioning"
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
            assert all(row["lease_owner"] is None for row in db.execute(rv.resource_locks.select().where(
                rv.resource_locks.c.resource_key.in_([token.resource_key for token in initial]))).mappings())
        assert worker.resume_one(Factory, oid, host, **expected)["state"] == "completed"
        count = launch.call_count
        assert worker.resume_one(Factory, oid, host, **expected)["status"] == "terminal"
        assert launch.call_count == count
        # A full initial request now follows the same recovery path after T1.
        # A failed T1 has nothing to release; retry reuses a committed row.
        request = request_for()
        with Factory() as db:
            balance_before = db.get(models.User, uid).balance
        def prepare_crash(db):
            row = db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).first()
            if row is not None and row.state == "prepared":
                raise RuntimeError("start T1 commit crash")
        event.listen(Factory.class_, "before_commit", prepare_crash)
        try:
            with patch.object(resource_leases, "release", wraps=resource_leases.release) as release_spy:
                try:
                    worker.start_once(Factory, request, host, **expected)
                    raise AssertionError("T1 crash missed")
                except RuntimeError:
                    pass
                assert release_spy.call_count == 0, "rolled-back T1 tokens were released"
        finally:
            event.remove(Factory.class_, "before_commit", prepare_crash)
        with Factory() as db:
            assert db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count() == 0
            assert db.get(models.User, uid).balance == balance_before
        started = worker.start_once(Factory, request, host, **expected)
        assert started["step"]["state"] == "remote_created"
        started_id = started["operation_id"]
        altered = request.copy(deep=True)
        altered.intent.sale.amount += 1
        altered.payers[0].amount += 1
        try:
            worker.start_once(Factory, altered, host, **expected)
            raise AssertionError("changed request replay accepted")
        except HTTPException as error:
            assert error.detail == "provisioning_request_changed"
        completed = worker.start_once(Factory, request, host, **expected)
        assert completed["operation_id"] == started_id and completed["state"] == "completed"
        count = launch.call_count
        assert worker.start_once(Factory, request, host, **expected)["status"] == "terminal"
        assert launch.call_count == count
        with Factory() as db:
            assert db.get(models.User, uid).balance == balance_before - 25
            assert db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count() == 1
            assert db.query(mp.PaymentReservation).filter_by(operation_id=started_id).count() == 1
        # A node disabled after T1 must not create/capture anything. Because
        # remote was never attempted, cleanup needs no node I/O even offline.
        oid, tokens = prepared()
        with Factory() as db:
            held = db.get(models.User, uid).balance
            db.get(models.Node, nid).enabled = False
            db.commit()
        count = launch.call_count
        assert tick(oid, tokens)["status"] == "compensation_started"
        with Factory() as db:
            assert db.get(mp.ProvisioningOperation, oid).error_code == "node_unavailable"
            assert db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one().state == "removed"
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
        assert tick(oid, tokens)["state"] == "compensated"
        assert launch.call_count == count
        with Factory() as db:
            assert db.get(models.User, uid).balance == held + 25
            db.get(models.Node, nid).enabled = True
            db.commit()
        release(tokens)
        # Epoch changes stop forward delivery, but cannot justify releasing
        # an old-generation hold through the current-generation wallet.
        oid, tokens = prepared()
        with Factory() as db:
            epoch = db.execute(rv.wallet_runtime_state.select()).mappings().one()["epoch"]
            held = db.get(models.User, uid).balance
            db.execute(rv.wallet_runtime_state.update().values(epoch=epoch + 1))
            db.commit()
        count = launch.call_count
        assert tick(oid, tokens)["status"] == "compensation_started"
        try:
            tick(oid, tokens)
            raise AssertionError("old epoch hold released through new epoch")
        except HTTPException as error:
            assert error.detail == "wallet_epoch_changed"
        with Factory() as db:
            assert db.get(mp.ProvisioningOperation, oid).error_code == "wallet_epoch_changed"
            assert db.get(models.User, uid).balance == held
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
            db.execute(rv.wallet_runtime_state.update().values(epoch=epoch))  # Restore OWNED test fixture only.
            db.commit()
        assert launch.call_count == count
        assert tick(oid, tokens)["state"] == "compensated"
        release(tokens)
        with Factory() as db:
            gone = wallet_accounts.create_user_with_wallet(db, id=100000,
                username="gone-target", balance=50, telegram_id=43)
            db.commit()
            gone_id = gone.id
        request = request_for()
        request.target_user_id = gone_id
        request.intent.user = final.UserSnapshot(username="gone-target", telegram_id=43)
        request.payers[0].id = gone_id
        with Factory() as db:
            resource_leases.begin_business(db)
            result = prep.prepare(db, request, identity=host, **expected)
            oid, tokens = result.operation.id, result.leases
            db.commit()
        with Factory() as db:
            # Owned scratch rows only: preserve the accounting snapshot while
            # severing its live FK before the target disappears after T1.
            # This models a tombstoned account on BOTH database dialects, not
            # a deletion that only works when SQLite FK enforcement is off.
            db.execute(rv.wallet_accounts.update().where(rv.wallet_accounts.c.user_id == gone_id).values(
                user_id=None, tombstoned_at=dt.datetime.utcnow()))
            db.execute(models.User.__table__.delete().where(models.User.id == gone_id))
            db.commit()
        count = launch.call_count
        assert tick(oid, tokens)["status"] == "compensation_started"
        with Factory() as db:
            assert db.get(mp.ProvisioningOperation, oid).error_code == "target_user_missing"
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
        assert launch.call_count == count
        release(tokens)  # No replacement customer / guessed refund is created.
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
            db.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key.in_(
                [token.resource_key for token in tokens])).values(leased_until=dt.datetime(2000, 1, 1)))
            db.commit()
        tokens = worker.reacquire(Factory, oid, host, **expected)
        with Factory() as db:
            assert db.query(mp.ProvisioningStep).filter_by(operation_id=oid).one().state == "remote_calling"
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).one().state == "reserved"
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
