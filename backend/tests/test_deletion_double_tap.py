"""Two deletions of the same customer, and the id SQLite gives back later.

Run:  python3 backend/tests/test_deletion_double_tap.py

Production, 2026-11-07..09: a double-tapped bot "delete" made delete_user
operations 511 and 512 for user 6018, 1.5 s apart. 511 completed; 512 was
left with a WireGuard step in remote_calling and could never finish (its
connection rows were gone), which also blocked update.sh. The next day
SQLite reused user id 6018 and connection ids 10188-10191 for OTHER
customers - 512 still named "user 6018".

  A. a second deletion of a scope that is still being deleted is refused;
  B. a stuck exact duplicate of a completed deletion is closed DB-only:
     nothing deleted (not the customer who now has that id), no remote call.
"""
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
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import provisioning_schema, receipt_void_schema, wallet_accounts
from app.services import provisioning_contracts as contracts, provisioning_delete_preparation as prep
from app.services import provisioning_deletion_worker as worker, provisioning_parent_execute as execute
from app.services import remote_action
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


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
        runtime.ownership_epoch, runtime.gate_mode_epoch, runtime.gate_mode = 1, 1, "enforced"
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        for kind in ("delete_connection", "delete_purchase", "delete_user"):
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        admin = models.AdminUser(username="owner", hashed_password="test", is_superadmin=True)
        node = models.Node(name="fake-node", type=models.NodeType.mikrotik, enabled=True,
                           mt_wireguard_interface="wg0", mt_host="192.0.2.1")
        db.add_all([admin, node]); db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=admin.id,
            verified_at=dt.datetime.utcnow()))
        nid = node.id
        db.commit()
    expected = dict(installation_uuid=installation, ownership_epoch=1)

    def customer(name):
        with Factory() as db:
            user = wallet_accounts.create_user_with_wallet(db, username=name)
            db.flush()
            conn = models.Connection(user_id=user.id, node_id=nid, type=models.ConnectionType.wireguard, enabled=True,
                                     wg_peer_name="user-" + name, wg_public_key="PUB-" + name,
                                     wg_client_address="10.0.0.2/32")
            db.add(conn); db.commit()
            return user.id, conn.id

    def request(uid, name):
        return prep.DeleteRequest(resource_kind="user", resource_id=uid, tenant_scope_key="shared",
                                  business_key=f"delete:user:{uid}:{uuid.uuid4()}", target_username=name)

    unknown = lambda dto: remote_action.RemoteActionResult.killed_unknown(dto.action_id, "hard_deadline_exceeded")
    absent = lambda dto: remote_action.RemoteActionResult(dto.action_id, remote_action.Outcome.ABSENT_VERIFIED,
                                                           write_attempted=True, remote_outcome="verified_absent")
    configured = {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}

    # A. the second tap while the first deletion is still open
    uid, cid = customer("tap")
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action", side_effect=unknown):
        first = worker.start_once(Factory, request(uid, "tap"), host, **expected)
        try:
            second = worker.start_once(Factory, request(uid, "tap"), host, **expected)
            got = ("created", second.get("operation_id"))
        except HTTPException as exc:
            got = (exc.status_code, exc.detail)
    check("A: a second deletion of a customer being deleted is refused", got, (409, "provisioning_deletion_in_progress"))
    with Factory() as db:
        check("A: only one deletion exists", db.query(mp.ProvisioningOperation).filter_by(
            operation_type="delete_user", target_user_id=uid).count(), 1)
        db.query(mp.ProvisioningStep).filter_by(operation_id=first["operation_id"]).update({"next_retry_at": None})
        db.commit()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action", side_effect=absent):
        for _ in range(4):
            if worker.resume_one(Factory, first["operation_id"], host, **expected).get("state") == "completed":
                break

    # B. the production shape: an identical twin left in remote_calling,
    #    the first one completed, then the ids given to a new customer
    uid, cid = customer("victim")
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action", side_effect=absent):
        done = worker.start_once(Factory, request(uid, "victim"), host, **expected)
        for _ in range(4):
            if worker.resume_one(Factory, done["operation_id"], host, **expected).get("state") == "completed":
                break
    with Factory() as db:
        peer = db.get(mp.ProvisioningOperation, done["operation_id"])
        check("B: the first deletion completed", peer.state, "completed")
        twin = mp.ProvisioningOperation(operation_type="delete_user", business_key=f"delete:user:{uid}:{uuid.uuid4()}",
            request_hash="c" * 64, tenant_scope_key=peer.tenant_scope_key, actor_kind=peer.actor_kind,
            actor_id=peer.actor_id, target_user_id=uid, intent=peer.intent, state="provisioning",
            wallet_epoch_at_start=peer.wallet_epoch_at_start, forward_deadline=peer.forward_deadline)
        db.add(twin); db.flush()
        for s in db.query(mp.ProvisioningStep).filter_by(operation_id=peer.id):
            db.add(mp.ProvisioningStep(operation_id=twin.id, slot_key=s.slot_key, step_order=s.step_order,
                connection_id=s.connection_id, node_id=s.node_id, protocol=s.protocol, backend=s.backend,
                direction="remove", state="remote_calling", remote_attempted=True, wg_interface=s.wg_interface,
                wg_peer_name=s.wg_peer_name, wg_public_key=s.wg_public_key, wg_client_address=s.wg_client_address))
        twin_id = twin.id
        db.commit()
    # SQLite hands the freed ids to the next customer (forced on MariaDB).
    with Factory() as db:
        newcomer = wallet_accounts.create_user_with_wallet(db, username="newcomer")
        db.flush()
        if newcomer.id != uid:
            db.rollback()
            newcomer = wallet_accounts.create_user_with_wallet(db, username="newcomer")
            newcomer.id = uid
        newcomer.created_at = dt.datetime.utcnow() + dt.timedelta(seconds=5)
        db.flush()
        conn = models.Connection(id=cid, user_id=uid, node_id=nid, type=models.ConnectionType.wireguard, enabled=True,
                                 wg_peer_name="user-newcomer", wg_public_key="PUB-newcomer",
                                 wg_client_address="10.0.0.3/32")
        db.add(conn); db.commit()
    with patch.dict(os.environ, configured), patch.object(execute.remote_runner, "run_action") as launch:
        try:
            outcome = worker.resume_one(Factory, twin_id, host, **expected)
            got = outcome.get("state")
        except HTTPException as exc:
            got = f"HTTP {exc.status_code} {exc.detail}"
        check("B: the stuck twin is closed", got, "completed")
        check("B: no remote call was made", launch.call_count, 0)
    with Factory() as db:
        check("B: the customer who now has that id is untouched",
              (db.get(models.User, uid).username, db.get(models.Connection, cid).wg_peer_name),
              ("newcomer", "user-newcomer"))
        check("B: the closed twin names no result user", db.get(mp.ProvisioningOperation, twin_id).result_user_id, None)

    # C. the guard does not close a deletion whose target still exists
    uid2, cid2 = customer("alive")
    with Factory() as db:
        from app.services import provisioning_deletion as deletion
        fake = mp.ProvisioningOperation(operation_type="delete_user", business_key=f"x:{uuid.uuid4()}",
            request_hash="d" * 64, tenant_scope_key="shared", actor_kind="system", target_user_id=uid2,
            intent=json.dumps({"resource_kind": "user", "resource_id": uid2, "connection_fingerprints": {}}),
            state="provisioning", wallet_epoch_at_start=0, forward_deadline=dt.datetime.utcnow())
        db.add(fake); db.flush()
        check("C: no completed twin -> not closed", deletion.complete_superseded(db, fake), False)
        db.rollback()
    print("done", engine.dialect.name)


with tempfile.TemporaryDirectory(prefix="um-delete-twice-") as directory:
    engine = create_engine("sqlite:///" + directory + "/twice.db")
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
else:
    print("SKIP real MariaDB locally")
if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall double-tap deletion checks passed")
