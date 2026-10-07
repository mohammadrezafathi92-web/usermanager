"""Transactional WG IP claims, rollback, lease fencing and real races."""
import datetime as dt
import os
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_schema, receipt_void_schema, resource_leases as locks
from app.services import provisioning_wireguard_pool as pool


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
    node = models.Node(name="pool", type=models.NodeType.mikrotik, enabled=True,
        mt_wireguard_interface="wg1", mt_client_subnet="10.0.0.0/30")
    user = models.User(username="pool-user")
    db.add_all([node, user])
    db.flush()
    db.add(models.Connection(user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard,
                             wg_client_address="10.0.0.2/32", enabled=False))
    def staged(node_id=node.id, **extra):
        operation = mp.ProvisioningOperation(operation_type="add_connection", business_key=str(uuid.uuid4()),
            request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}", state="prepared",
            target_user_id=user.id, wallet_epoch_at_start=0, forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
        db.add(operation)
        db.flush()
        values = dict(operation_id=operation.id, slot_key="connection:0", direction="create", step_order=0,
                      backend="mikrotik_wg", protocol="wireguard", node_id=node_id, wg_interface="wg1", state="staged")
        values.update(extra)
        step = mp.ProvisioningStep(**values)
        db.add(step)
        db.flush()
        return step.id, operation.id
    sid, oid = staged()
    db.commit()
    nid = node.id
    db.rollback()
    def tokens(operation_id):
        result = [locks.acquire(db, f"provisioning_op:{operation_id}", f"op:{operation_id}:1"),
                  locks.acquire(db, f"node:{nid}:wg_pool", f"op:{operation_id}:1")]
        db.commit()
        return result
    leases = tokens(oid)
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))
    locks.begin_business(db)
    refused("provisioning_lease_scope_invalid", lambda: pool.allocate(db, sid, 0, leases[:1]))
    db.rollback()
    locks.begin_business(db)
    stale = [locks.Lease(leases[0].resource_key, leases[0].owner, leases[0].epoch + 1), leases[1]]
    try:
        pool.allocate(db, sid, 0, stale)
        raise AssertionError("stale lease accepted")
    except locks.LeaseLost:
        pass
    db.rollback()
    locks.begin_business(db)
    step = pool.allocate(db, sid, 0, leases, count=2)
    assert step.wg_client_address == "10.0.0.3/32,10.0.0.4/32" and step.wg_subnet_expanded
    assert db.get(models.Node, nid).mt_client_subnet == "10.0.0.0/29" and not commits
    db.rollback()
    assert db.get(models.Node, nid).mt_client_subnet == "10.0.0.0/30"
    assert db.get(mp.ProvisioningStep, sid).wg_client_address is None
    db.rollback()
    locks.begin_business(db)
    step = pool.allocate(db, sid, 0, leases, count=2)
    version, address = step.version, step.wg_client_address
    assert pool.allocate(db, sid, version, leases, count=2).wg_client_address == address
    refused("wg_pool_claim_changed", lambda: pool.allocate(db, sid, version, leases))
    refused("wg_pool_count_invalid", lambda: pool.allocate(db, sid, version, leases, True))
    db.commit()
    assert len(commits) == 1
    for token in leases:
        assert locks.release(db, token)
    second, second_oid = staged()
    db.commit()
    leases = tokens(second_oid)
    locks.begin_business(db)
    step = pool.allocate(db, second, 0, leases)
    assert step.wg_client_address == "10.0.0.5/32"  # in-flight claims reserved
    db.rollback()
    # An abandoned resource remains reserved even when its operation is terminal.
    db.query(mp.ProvisioningStep).filter_by(id=sid).update(dict(state="removed", remote_attempted=True,
        remote_outcome="abandoned", forced_by_admin_id=1, force_reason="staging test only", forced_at=dt.datetime.utcnow()))
    db.query(mp.ProvisioningOperation).filter_by(id=oid).update(dict(state="compensated", completed_at=dt.datetime.utcnow()))
    db.commit()
    locks.begin_business(db)
    assert pool.allocate(db, second, 0, leases).wg_client_address == "10.0.0.5/32"
    db.rollback()
    db.query(mp.ProvisioningStep).filter_by(id=sid).update(dict(remote_outcome="verified_absent",
        forced_by_admin_id=None, force_reason=None, forced_at=None))
    db.commit()
    locks.begin_business(db)
    assert pool.allocate(db, second, 0, leases).wg_client_address == "10.0.0.3/32"
    db.rollback()
    db.query(models.Node).filter_by(id=nid).update(dict(mt_wireguard_interface="other"))
    db.commit()
    locks.begin_business(db)
    refused("wg_pool_node_changed", lambda: pool.allocate(db, second, 0, leases))
    db.rollback()
    db.query(models.Node).filter_by(id=nid).update(dict(mt_wireguard_interface="wg1"))
    db.query(models.Connection).filter_by(node_id=nid).update(dict(wg_client_address="not-an-ip"))
    db.commit()
    locks.begin_business(db)
    refused("wg_pool_address_invalid", lambda: pool.allocate(db, second, 0, leases))
    db.rollback()
    db.query(models.Connection).filter_by(node_id=nid).update(dict(wg_client_address="10.0.0.2/32"))
    db.commit()
    locks.begin_business(db)
    refused("wg_pool_claim_too_long", lambda: pool.allocate(db, second, 0, leases, count=10))
    db.rollback()
    assert db.get(models.Node, nid).mt_client_subnet == "10.0.0.0/29"
    db.rollback()
    db.execute(rv.wallet_runtime_state.update().values(epoch=1))
    db.commit()
    locks.begin_business(db)
    refused("wallet_epoch_changed", lambda: pool.allocate(db, second, 0, leases))
    db.rollback()
    db.execute(rv.wallet_runtime_state.update().values(epoch=0))
    for token in leases:
        locks.release(db, token)
    db.commit()
    # Two real sessions compete for the node lease, then retry losers. Every
    # successful T1 sees the committed IP claims of the previous holder.
    race_node = models.Node(name="race", type=models.NodeType.mikrotik, enabled=True,
        mt_wireguard_interface="wg1", mt_client_subnet="10.1.0.0/24")
    db.add(race_node)
    db.flush()
    ids = [staged(race_node.id), staged(race_node.id)]
    race_nid = race_node.id
    db.commit()
    db.close()
    barrier = threading.Barrier(2)
    outcomes, errors = [], []
    def compete(pair):
        session = Factory()
        local_tokens = []
        try:
            sid, oid = pair
            local_tokens.append(locks.acquire(session, f"provisioning_op:{oid}", f"op:{oid}:1"))
            session.commit()
            barrier.wait(timeout=10)
            try:
                local_tokens.append(locks.acquire(session, f"node:{race_nid}:wg_pool", f"op:{oid}:1"))
                session.commit()
            except locks.LeaseBusy:
                session.rollback()
                outcomes.append("busy")
                return
            locks.begin_business(session)
            result = pool.allocate(session, sid, 0, local_tokens)
            outcomes.append(result.wg_client_address)
            session.commit()
        except Exception as exc:
            errors.append(type(exc).__name__)
            session.rollback()
        finally:
            for token in local_tokens:
                locks.release(session, token)
            session.commit()
            session.close()
    threads = [threading.Thread(target=compete, args=(pair,)) for pair in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and not any(thread.is_alive() for thread in threads), errors
    addresses = [value for value in outcomes if value != "busy"]
    assert addresses and len(addresses) == len(set(addresses)), outcomes
    # A loser retries its complete transaction after the holder releases.
    with Factory() as db:
        for sid, oid in ids:
            if db.get(mp.ProvisioningStep, sid).wg_client_address:
                continue
            db.rollback()
            retry = [locks.acquire(db, f"provisioning_op:{oid}", f"op:{oid}:2"),
                     locks.acquire(db, f"node:{race_nid}:wg_pool", f"op:{oid}:2")]
            db.commit()
            locks.begin_business(db)
            pool.allocate(db, sid, 0, retry)
            db.commit()
            for token in retry:
                locks.release(db, token)
            db.commit()
        values = [db.get(mp.ProvisioningStep, sid).wg_client_address for sid, oid in ids]
        assert len(set(values)) == 2 and all(values), values
    print("PASS", engine.dialect.name, "rollback, staged/abandoned claims, fencing and two-session disjoint allocation")


with tempfile.TemporaryDirectory(prefix="um-wg-pool-") as directory:
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
