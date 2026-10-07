"""Fenced deletion completion: exact scope, absence proof, rollback and audit."""
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
from app.services import provisioning_schema, receipt_void_schema, wallet_accounts
from app.services import provisioning_deletion as deletion, provisioning_transitions as transitions
from app.services import resource_leases as locks
from app.services.provisioning_identity import removal_matches, XRAY_MODES

# Typed identity gate covers every panel backend even though the deletion
# transaction fixtures use WireGuard and no-remote PPP, not real nodes.
for backend, mode in XRAY_MODES.items():
    node = models.Node(id=1, type=models.NodeType.xray, xr_panel_mode=mode, xr_inbound_tag="tag", xr_panel_inbound_id=2)
    connection = models.Connection(node_id=1, type=models.ConnectionType.xray, xr_email="email", xr_uuid="UUID_SECRET")
    step = mp.ProvisioningStep(node_id=1, protocol="xray", backend=backend, xr_email="email",
        xr_inbound_tag="tag", xr_panel_inbound_id=2, staged_xr_uuid="UUID_SECRET")
    assert removal_matches(step, connection, node)
    step.staged_xr_uuid = "different-object"
    assert not removal_matches(step, connection, node)
    step.staged_xr_uuid = None  # cleared only after confirmed removal
    assert removal_matches(step, connection, node, removed=True)
    step.xr_panel_inbound_id = 3
    assert not removal_matches(step, connection, node, removed=True)
node = models.Node(id=1, type=models.NodeType.softether)
connection = models.Connection(node_id=1, type=models.ConnectionType.softether, ppp_username="account")
step = mp.ProvisioningStep(node_id=1, protocol="softether", backend="softether", account_username="account")
assert removal_matches(step, connection, node)
step.account_username = "other-account"
assert not removal_matches(step, connection, node)


def refused(code, callback):
    try:
        callback()
        raise AssertionError("unexpected acceptance")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    for kind in ("connection", "purchase", "user"):
        db = Factory()
        user = wallet_accounts.create_user_with_wallet(db, username="delete-" + str(uuid.uuid4()), balance=100,
            status=models.UserStatus.disabled if kind == "user" else models.UserStatus.active,
            purchases_blocked=kind == "user")
        node = models.Node(name="node", type=models.NodeType.mikrotik, mt_wireguard_interface="wg1")
        db.add(node)
        db.flush()
        purchases = [models.Purchase(user_id=user.id, quota_bytes=1000), models.Purchase(user_id=user.id, quota_bytes=2000)]
        db.add_all(purchases)
        db.flush()
        connections = [models.Connection(user_id=user.id, node_id=node.id, purchase_id=purchases[0].id,
            type=models.ConnectionType.wireguard, enabled=False, wg_peer_name="peer", wg_public_key="public",
            wg_client_address="10.0.0.2/32"), models.Connection(user_id=user.id, node_id=node.id,
            purchase_id=purchases[1].id, type=models.ConnectionType.pptp, enabled=False, ppp_username="ppp")]
        db.add_all(connections)
        db.flush()
        selected = connections if kind == "user" else connections[:1]
        ident = user.id if kind == "user" else purchases[0].id if kind == "purchase" else connections[0].id
        operation = mp.ProvisioningOperation(operation_type="delete_" + kind, business_key=str(uuid.uuid4()),
            request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", target_user_id=user.id,
            intent=deletion.snapshot(kind, ident, selected, purchase_ids=[row.id for row in purchases]),
            state="remote_complete", wallet_epoch_at_start=0,
            forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
        db.add(operation)
        assert "UUID_SECRET" not in operation.intent and "public" not in operation.intent
        db.flush()
        for i, connection in enumerate(selected):
            db.add(mp.ProvisioningStep(operation_id=operation.id, slot_key=f"connection:{connection.id}",
                direction="remove", step_order=i, backend="mikrotik_wg" if i == 0 else "radius_ppp",
                protocol=connection.type.value, node_id=node.id, connection_id=connection.id,
                wg_interface=node.mt_wireguard_interface, wg_peer_name=connection.wg_peer_name,
                wg_public_key=connection.wg_public_key, wg_client_address=connection.wg_client_address,
                account_username=connection.ppp_username,
                state="removed", remote_attempted=i == 0, remote_outcome="verified_absent" if i == 0 else None))
        ledger = models.LedgerEntry(kind="sale_new", amount=50, user_id=user.id, purchase_id=purchases[0].id,
                                   username_snapshot=user.username)
        usage = models.UsageLog(user_id=user.id, connection_id=connections[0].id)
        audit = models.RadiusLimitEventLog(user_id=user.id, connection_id=connections[0].id, username="snapshot")
        db.add_all([ledger, usage, audit])
        db.commit()
        uid, oid, cid, sibling, pid, sibling_pid = user.id, operation.id, connections[0].id, connections[1].id, purchases[0].id, purchases[1].id
        ledger_id, usage_id, audit_id = ledger.id, usage.id, audit.id
        account = db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.user_id == uid)).mappings().one()
        identity, account_id = account["customer_identity_id"], account["id"]
        db.rollback()
        tokens = [locks.acquire(db, f"customer_identity:{identity}", f"op:{oid}:1"),
                  locks.acquire(db, f"provisioning_op:{oid}", f"op:{oid}:1")]
        db.commit()
        commits = []
        event.listen(db, "after_commit", lambda session: commits.append(True))
        locks.begin_business(db)
        refused("provisioning_lease_scope_invalid", lambda: deletion.finish(db, oid, 0, tokens[:1]))
        db.rollback()
        locks.begin_business(db)
        refused("provisioning_lease_scope_invalid", lambda: deletion.finish(db, oid, 0, tokens[1:]))
        db.rollback()
        locks.begin_business(db)
        stale = [locks.Lease(tokens[0].resource_key, tokens[0].owner, tokens[0].epoch + 1), tokens[1]]
        try:
            deletion.finish(db, oid, 0, stale)
            raise AssertionError("stale lease accepted")
        except locks.LeaseLost:
            db.rollback()
        locks.begin_business(db)
        refused("provisioning_operation_changed", lambda: deletion.finish(db, oid, 99, tokens))
        db.rollback()
        locks.begin_business(db)
        db.get(models.Connection, cid).wg_public_key = "different-public"
        db.flush()
        refused("provisioning_deletion_scope_changed", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        db.get(models.Node, node.id).mt_host = "changed-router"
        db.flush()
        refused("provisioning_deletion_scope_changed", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        db.get(models.Connection, cid).enabled = True
        db.flush()
        refused("provisioning_deletion_not_disabled", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid, connection_id=cid).one()
        step.state, step.remote_outcome = "cleanup_required", "unverified"
        db.flush()
        refused("provisioning_steps_incomplete", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid, connection_id=cid).one()
        step.remote_outcome, step.forced_by_admin_id, step.force_reason, step.forced_at = (
            "abandoned", 1, "test force", dt.datetime.utcnow())
        db.flush()
        refused("provisioning_steps_incomplete", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid, connection_id=cid).one()
        step.wg_public_key = "different-peer-was-removed"
        db.flush()
        refused("provisioning_steps_incomplete", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        locks.begin_business(db)
        step = db.query(mp.ProvisioningStep).filter_by(operation_id=oid, connection_id=cid).one()
        step.backend, step.remote_attempted, step.remote_outcome = "radius_ppp", False, None
        db.flush()
        refused("provisioning_steps_incomplete", lambda: deletion.finish(db, oid, 0, tokens))
        db.rollback()
        if kind == "user":
            locks.begin_business(db)
            db.add(models.Purchase(user_id=uid, quota_bytes=100))
            db.flush()
            refused("provisioning_deletion_scope_changed", lambda: deletion.finish(db, oid, 0, tokens))
            db.rollback()
        if kind != "connection":
            locks.begin_business(db)
            db.add(models.Connection(user_id=uid, node_id=node.id, purchase_id=pid,
                type=models.ConnectionType.pptp, ppp_username="added-later", enabled=False))
            db.flush()
            refused("provisioning_deletion_scope_changed", lambda: deletion.finish(db, oid, 0, tokens))
            db.rollback()
        locks.begin_business(db)
        # Crash after DB deletions but before operation completion. Every
        # detach/delete/tombstone must roll back with the failed final CAS.
        with patch.object(transitions, "_write_operation", side_effect=RuntimeError("final CAS crash")):
            try:
                deletion.finish(db, oid, 0, tokens)
                raise AssertionError("crash not reached")
            except RuntimeError:
                db.rollback()
        assert db.get(models.Connection, cid) is not None and db.get(models.User, uid).balance == 100
        assert db.get(models.LedgerEntry, ledger_id).purchase_id == pid
        assert db.get(models.UsageLog, usage_id).connection_id == cid
        assert db.execute(select(rv.wallet_accounts.c.user_id).where(rv.wallet_accounts.c.id == account_id)).scalar_one() == uid
        assert db.get(mp.ProvisioningOperation, oid).state == "remote_complete" and commits == []
        db.rollback()
        locks.begin_business(db)
        deletion.finish(db, oid, 0, tokens)
        assert commits == []
        db.commit()
        db.expire_all()
        assert db.get(models.Connection, cid) is None
        assert db.get(models.LedgerEntry, ledger_id).amount == 50
        assert db.get(models.RadiusLimitEventLog, audit_id).connection_id is None
        if kind == "user":
            assert db.get(models.User, uid) is None and db.get(models.Connection, sibling) is None
            assert db.execute(select(rv.wallet_accounts.c.user_id).where(rv.wallet_accounts.c.id == account_id)).scalar_one() is None
        else:
            assert db.get(models.User, uid).balance == 100 and db.get(models.Connection, sibling) is not None
            assert db.get(models.Purchase, sibling_pid) is not None
            assert db.get(models.UsageLog, usage_id).connection_id is None
            assert (db.get(models.Purchase, pid) is None) == (kind == "purchase")
        db.rollback()
        locks.begin_business(db)
        assert deletion.finish(db, oid, 0, tokens).state == "completed"
        db.commit()
        db.close()
        print("PASS", engine.dialect.name, kind, "scope, proof, fence, crash rollback, audit retention and idempotent completion")


with tempfile.TemporaryDirectory(prefix="um-deletion-final-") as directory:
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
