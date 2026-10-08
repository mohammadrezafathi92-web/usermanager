"""Committed backoff fences private dispatch before markers/credentials.

Owned disposable SQLite and mandatory real MariaDB; no child/node I/O.
"""
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
from app.services import provisioning_schema, receipt_void_schema, resource_leases
from app.services import provisioning_parent_dispatch as dispatch
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    now = dt.datetime.utcnow()
    future = now + dt.timedelta(days=2)
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = now
        runtime.ownership_epoch, runtime.gate_mode = 1, "enforced"  # Owned fixture only.
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.get(mp.ProvisioningTypeMode, "purchase").mode = "durable"
        db.add(models.PanelSettings(id=1, ha_enabled=False))
        db.commit()
        for state in ("staged", "remote_calling", "compensating"):
            for delayed in ("operation", "step"):
                operation = mp.ProvisioningOperation(operation_type="purchase", business_key=state + delayed,
                    request_hash="b" * 64, tenant_scope_key="global", actor_kind="system", intent="{}",
                    state="compensating" if state == "compensating" else "provisioning",
                    forward_deadline=future, wallet_epoch_at_start=0,
                    next_retry_at=future if delayed == "operation" else None)
                db.add(operation)
                db.flush()
                step = mp.ProvisioningStep(operation_id=operation.id, slot_key="main", step_order=0,
                    direction="create", backend="mikrotik_wg", node_id=1, protocol="wireguard",
                    state=state, remote_attempted=state != "staged",
                    next_retry_at=future if delayed == "step" else None)
                db.add(step)
                db.commit()
                token = resource_leases.acquire(db, f"provisioning_op:{operation.id}", f"op:{operation.id}:1")
                db.commit()
                oid, sid, version = operation.id, step.id, step.version
                saved = (operation.state, operation.version, step.state, step.version, step.remote_attempted)
                db.rollback()
                statements = []
                def capture(_conn, _cursor, statement, _parameters, _context, _many):
                    statements.append(statement)
                event.listen(engine, "before_cursor_execute", capture)
                try:
                    resource_leases.begin_business(db)
                    try:
                        if state == "compensating":
                            dispatch.compensation_snapshot(db, sid, version, host, [token])
                        else:
                            dispatch.snapshot(db, sid, version, host, [token], recovery_read=state == "remote_calling")
                        raise AssertionError("future backoff accepted by direct dispatch")
                    except HTTPException as error:
                        assert (error.status_code, error.detail) == (409, "provisioning_retry_not_due")
                    finally:
                        db.rollback()
                finally:
                    event.remove(engine, "before_cursor_execute", capture)
                assert not any("FROM nodes" in sql for sql in statements), "management credentials loaded before backoff"
                operation = db.get(mp.ProvisioningOperation, oid)
                step = db.get(mp.ProvisioningStep, sid)
                assert (operation.state, operation.version, step.state, step.version, step.remote_attempted) == saved
                assert resource_leases.release(db, token)
                db.commit()
    assert _no_network.attempts == []
    print("PASS", engine.dialect.name, "operation/step backoff fences first send, recovery and compensation")


with tempfile.TemporaryDirectory(prefix="um-dispatch-retry-") as directory:
    engine = create_engine("sqlite:///" + directory + "/retry.db")
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
