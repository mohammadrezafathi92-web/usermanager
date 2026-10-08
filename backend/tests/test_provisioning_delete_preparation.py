"""Private deletion T1: atomic scope/idempotence, no remote or live wiring."""
import datetime as dt
import json
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
from app.services import provisioning_delete_preparation as prep, provisioning_deletion as deletion
from app.services import provisioning_schema, receipt_void_schema, provisioning_contracts as contracts
from app.services import wallet_accounts, resource_leases as locks
from app.services import provisioning_transitions as transitions
from app.services.adapter_base import AbsentOutcome
from app.services.provisioning_host import HostIdentity


def refused(code, callback):
    try:
        callback()
        raise AssertionError("unexpected acceptance")
    except HTTPException as error:
        assert error.detail == code, (error.detail, code)


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        arguments = dict(identity=host, installation_uuid=runtime.installation_uuid, ownership_epoch=1)
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
        runtime.ownership_epoch, runtime.gate_mode = 1, "enforced"  # Owned scratch only.
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        admin = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
        db.add_all([admin, models.PanelSettings(id=1, ha_enabled=False)])
        for kind in ("delete_user", "delete_purchase", "delete_connection"):
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        aid = admin.id

        def begin():
            db.rollback()  # End fixture assertion reads before the fenced writer.
            locks.begin_business(db)

        def fixture(kind):
            user = wallet_accounts.create_user_with_wallet(db, username="delete-" + uuid.uuid4().hex[:12], balance=101)
            purchase = models.Purchase(user_id=user.id, quota_bytes=123456, used_bytes=456)
            sibling = models.Purchase(user_id=user.id, quota_bytes=654321)
            db.add_all([purchase, sibling])
            db.flush()
            rows, nodes = [], []
            for backend in ("mikrotik_wg", "radius_ppp", "softether", *contracts.XRAY_MODES):
                node = models.Node(name=backend, type=models.NodeType.mikrotik if backend in ("mikrotik_wg", "radius_ppp")
                    else models.NodeType.softether if backend == "softether" else models.NodeType.xray,
                    enabled=False, mt_wireguard_interface="wg1", xr_panel_mode=contracts.XRAY_MODES.get(backend),
                    xr_panel_inbound_id=2, xr_inbound_tag="tag")
                db.add(node)
                db.flush()
                if backend != "radius_ppp":
                    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend=backend, state="ready",
                        adapter_version=contracts.adapter_version(), server_fingerprint="b" * 64,
                        config_fingerprint=contracts.config_fingerprint(node), contract='{"not_exist":[]}',
                        verified_by_admin_id=aid, verified_at=dt.datetime.utcnow()))
                connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
                    type=models.ConnectionType.wireguard if backend == "mikrotik_wg" else models.ConnectionType.pptp
                    if backend == "radius_ppp" else models.ConnectionType.softether if backend == "softether"
                    else models.ConnectionType.xray, enabled=True, wg_peer_name="peer", wg_public_key="PUBLIC",
                    wg_client_address="10.0.0.2/32", wg_private_key="PRIVATE-NEVER-STAGED", ppp_username="account",
                    ppp_password="PASSWORD-NEVER-STAGED", xr_email="email", xr_uuid="XR-IDENTITY")
                db.add(connection)
                rows.append(connection)
                nodes.append(node)
            other = models.Connection(user_id=user.id, purchase_id=sibling.id, node_id=nodes[1].id,
                type=models.ConnectionType.pptp, ppp_username="sibling", enabled=True)
            ledger = models.LedgerEntry(kind="sale_new", amount=50, user_id=user.id, purchase_id=purchase.id)
            db.add_all([other, ledger])
            db.commit()
            ident = user.id if kind == "user" else purchase.id if kind == "purchase" else rows[0].id
            return user, rows, other, ledger, prep.DeleteRequest(resource_kind=kind, resource_id=ident,
                tenant_scope_key="shared", business_key=str(uuid.uuid4()))

        for kind in ("connection", "purchase", "user"):
            user, rows, other, ledger, request = fixture(kind)
            uid, lid = user.id, ledger.id
            selected = [*rows, other] if kind == "user" else rows if kind == "purchase" else rows[:1]
            expected_ids = {row.id for row in selected}
            commits = []
            def committed(_session):
                commits.append(True)
            event.listen(db, "after_commit", committed)
            begin()
            result = prep.prepare(db, request, **arguments)
            oid = result.operation.id
            assert not commits, "hidden commit in deletion T1"
            steps = db.query(mp.ProvisioningStep).filter_by(operation_id=oid).all()
            assert {step.connection_id for step in steps} == expected_ids
            assert all(step.direction == "remove" and step.state == "staged" and not step.remote_attempted for step in steps)
            assert all(step.staged_wg_private_key is None and step.staged_password is None for step in steps)
            assert all(deletion.removal_matches(step, db.get(models.Connection, step.connection_id),
                db.get(models.Node, step.node_id)) for step in steps)
            assert all(secret not in result.operation.intent for secret in ("PRIVATE", "PASSWORD", "XR-IDENTITY"))
            assert json.loads(result.operation.intent)["resource_kind"] == kind
            assert all(not row.enabled for row in selected) and other.enabled == (kind != "user")
            assert user.purchases_blocked == (kind == "user")
            assert user.status == (models.UserStatus.disabled if kind == "user" else models.UserStatus.active)
            assert user.balance == 101 and db.get(models.LedgerEntry, lid) is not None
            assert db.query(mp.PaymentReservation).filter_by(operation_id=oid).count() == 0
            db.rollback()
            for step in steps:
                if step in db:
                    db.expunge(step)  # Discard scratch children queried after a rolled-back INSERT.
            assert not commits and db.get(mp.ProvisioningOperation, oid) is None
            assert db.get(models.User, uid).status == models.UserStatus.active and not db.get(models.User, uid).purchases_blocked
            assert all(db.get(models.Connection, cid).enabled for cid in expected_ids)
            begin()
            result = prep.prepare(db, request, **arguments)
            oid, tokens, saved_intent = result.operation.id, result.leases, result.operation.intent
            db.commit()
            assert len(commits) == 1
            begin()
            replay = prep.prepare(db, request, **arguments)
            assert replay.replay and replay.operation.id == oid and not replay.leases and replay.operation.intent == saved_intent
            db.rollback()
            begin()
            refused("provisioning_request_changed", lambda: prep.prepare(db,
                request.copy(update={"resource_id": request.resource_id + 100000}), **arguments))
            db.rollback()
            assert db.get(models.LedgerEntry, lid) is not None and db.get(models.User, uid).balance == 101
            # Exercise the REAL T1 snapshot through removal transitions and
            # the DB finalizer, not manually assembled "already removed" rows.
            # Results are typed injected observations; no remote is contacted.
            selected_ids = sorted(expected_ids)
            other_id = other.id
            purchase_id = ledger.purchase_id
            begin()
            refused("provisioning_operation_changed", lambda: deletion.finish(db, oid,
                db.get(mp.ProvisioningOperation, oid).version, tokens))
            db.rollback()
            for index, cid in enumerate(selected_ids):
                begin()
                step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid, connection_id=cid).one()
                transitions.begin_remove(db, step.id, step.version)
                db.commit()  # Attempt marker is durable before any observation.
                sid = step.id
                if step.backend != "radius_ppp":
                    if index == 0:
                        begin()
                        step = db.get(mp.ProvisioningStep, sid)
                        transitions.confirm_removed(db, sid, step.version, AbsentOutcome.UNVERIFIED)
                        db.commit()
                        assert db.get(mp.ProvisioningOperation, oid).state == "cleanup_required"
                        assert all(db.get(models.Connection, ident) is not None for ident in selected_ids)
                        assert db.get(models.LedgerEntry, lid).amount == 50
                        begin()
                        refused("provisioning_operation_transition_invalid", lambda: transitions.mark_remote_complete(db, oid))
                        db.rollback()
                        begin()
                        step = db.get(mp.ProvisioningStep, sid)
                        transitions.begin_remove(db, sid, step.version)
                        db.commit()
                    begin()
                    step = db.get(mp.ProvisioningStep, sid)
                    transitions.confirm_removed(db, sid, step.version, AbsentOutcome.VERIFIED_ABSENT)
                    db.commit()
                assert db.get(mp.ProvisioningStep, sid).state == "removed"
                assert db.get(mp.ProvisioningStep, sid).staged_xr_uuid is None
            begin()
            complete = transitions.mark_remote_complete(db, oid)
            db.commit()
            final_version = complete.version
            # A crash at final commit must restore every record and the
            # permanent financial row, not leave half a deleted service.
            def final_crash(_session):
                raise RuntimeError("injected deletion final commit failure")
            begin()
            deletion.finish(db, oid, final_version, tokens)
            event.listen(db, "before_commit", final_crash)
            try:
                try:
                    db.commit()
                    raise AssertionError("final commit fault missed")
                except RuntimeError:
                    db.rollback()
            finally:
                event.remove(db, "before_commit", final_crash)
            assert db.get(mp.ProvisioningOperation, oid).state == "remote_complete"
            assert db.get(models.User, uid) is not None
            assert all(db.get(models.Connection, ident) is not None for ident in selected_ids)
            assert db.get(models.LedgerEntry, lid).amount == 50
            begin()
            deletion.finish(db, oid, final_version, tokens)
            db.commit()
            assert db.get(mp.ProvisioningOperation, oid).state == "completed"
            assert all(db.get(models.Connection, ident) is None for ident in selected_ids)
            assert db.get(models.LedgerEntry, lid).amount == 50
            if kind == "user":
                assert db.get(models.User, uid) is None and db.get(models.Purchase, purchase_id) is None
                assert db.get(models.LedgerEntry, lid).user_id is None
                assert db.scalar(select(rv.wallet_accounts.c.id).where(rv.wallet_accounts.c.user_id == uid)) is None
            else:
                assert db.get(models.User, uid).balance == 101
                assert db.get(models.Connection, other_id).enabled
                assert (db.get(models.Purchase, purchase_id) is None) == (kind == "purchase")
            begin()
            assert deletion.finish(db, oid, final_version, tokens).state == "completed"
            db.rollback()  # Terminal retry cannot repeat any deletion/refund.
            for token in tokens:
                assert locks.release(db, token)
            db.commit()
            event.remove(db, "after_commit", committed)

        user, rows, other, ledger, request = fixture("user")
        begin()
        refused("provisioning_deletion_scope_changed", lambda: prep.prepare(db,
            request.copy(update={"tenant_scope_key": "foreign"}), **arguments))
        db.rollback()
        customer = db.scalar(select(rv.wallet_accounts.c.customer_identity_id).where(rv.wallet_accounts.c.user_id == user.id))
        blocker = locks.acquire(db, f"customer_identity:{customer}", "op:999999:1")
        db.commit()
        begin()
        try:
            prep.prepare(db, request, **arguments)
            raise AssertionError("live customer lease stolen")
        except locks.LeaseBusy:
            db.rollback()
        assert db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count() == 0
        assert all(row.enabled for row in rows) and not user.purchases_blocked and user.balance == 101
        assert locks.release(db, blocker)
        db.commit()
        original_scope = prep._scope
        def altered_scope(session, selected_request, **kw):
            result = original_scope(session, selected_request, **kw)
            if not kw["lock"]:
                session.add(models.Connection(user_id=user.id, node_id=rows[1].node_id,
                    type=models.ConnectionType.pptp, enabled=True, ppp_username="changed-after-preflight"))
                session.flush()
            return result
        begin()
        with patch.object(prep, "_scope", side_effect=altered_scope):
            refused("provisioning_deletion_scope_changed", lambda: prep.prepare(db, request, **arguments))
        db.rollback()
        assert db.query(models.Connection).filter_by(ppp_username="changed-after-preflight").count() == 0
        assert all(row.enabled for row in rows) and not user.purchases_blocked
        contract = db.get(mp.ProvisioningNodeContract, (rows[0].node_id, "mikrotik_wg"))
        contract.state, contract.invalidation_reason, contract.invalidated_at = "invalidated", "config_changed", dt.datetime.utcnow()
        db.commit()
        begin()
        refused("node_contract_not_ready", lambda: prep.prepare(db, request, **arguments))
        db.rollback()
        assert all(row.enabled for row in rows) and not user.purchases_blocked
        contract.state, contract.invalidation_reason, contract.invalidated_at = "ready", None, None
        db.commit()
        def crash(_session):
            raise RuntimeError("injected delete prepare commit failure")
        begin()
        prep.prepare(db, request, **arguments)
        event.listen(db, "before_commit", crash)
        try:
            try:
                db.commit()
                raise AssertionError("commit fault missed")
            except RuntimeError:
                db.rollback()
        finally:
            event.remove(db, "before_commit", crash)
        assert db.query(mp.ProvisioningOperation).filter_by(business_key=request.business_key).count() == 0
        assert all(row.enabled for row in rows) and not user.purchases_blocked and user.balance == 101
        empty_user = wallet_accounts.create_user_with_wallet(db, username="empty-" + uuid.uuid4().hex[:12], balance=77)
        db.commit()
        empty_request = prep.DeleteRequest(resource_kind="user", resource_id=empty_user.id,
            tenant_scope_key="shared", business_key=str(uuid.uuid4()))
        begin()
        empty = prep.prepare(db, empty_request, **arguments)
        assert db.query(mp.ProvisioningStep).filter_by(operation_id=empty.operation.id).count() == 0
        assert json.loads(empty.operation.intent)["purchase_ids"] == []
        assert empty_user.purchases_blocked and empty_user.balance == 77
        db.rollback()
        assert not empty_user.purchases_blocked and empty_user.status == models.UserStatus.active
        for column, value, code in (("gate_mode", "shadow", "gate_not_enforced"), ("owner_state", "draining", "provisioning_draining")):
            runtime = db.get(mp.ProvisioningRuntimeState, 1)
            old = getattr(runtime, column)
            setattr(runtime, column, value)
            db.commit()
            begin()
            refused(code, lambda: prep.prepare(db, request, **arguments))
            db.rollback()
            setattr(runtime, column, old)
            db.commit()
        db.get(models.PanelSettings, 1).ha_enabled = True
        db.commit()
        begin()
        refused("provisioning_ha_unsupported", lambda: prep.prepare(db, request, **arguments))
        db.rollback()
        assert _no_network.attempts == []
    print("PASS", engine.dialect.name, "atomic deletion, eight adapters+PPP, replay, rollback, leases and readiness")


with tempfile.TemporaryDirectory(prefix="um-delete-prepare-") as directory:
    engine = create_engine("sqlite:///" + directory + "/prepare.db")
    @event.listens_for(engine, "connect")
    def sqlite_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")
    try:
        scenario(engine)
    finally:
        engine.dispose()
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
