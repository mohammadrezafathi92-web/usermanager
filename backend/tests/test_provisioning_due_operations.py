"""Bounded, read-only due selection on disposable SQLite / mandatory MariaDB."""
import datetime as dt
import os
import re
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp, models_receipt_void as rv
from app.services import provisioning_schema, receipt_void_schema, resource_leases
from app.services import provisioning_due_operations as due
from app.services.provisioning_host import HostIdentity


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    host = HostIdentity("a" * 64, str(uuid.uuid4()))
    now = dt.datetime.utcnow()
    old, future = now - dt.timedelta(days=2), now + dt.timedelta(days=2)
    ids = {}
    with Factory() as db:
        runtime = db.get(mp.ProvisioningRuntimeState, 1)
        expected = dict(installation_uuid=runtime.installation_uuid, ownership_epoch=1)
        runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", host.host_id, host.boot_id
        runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = now
        runtime.ownership_epoch, runtime.gate_mode = 1, "enforced"  # Owned scratch fixture only.
        runtime.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        for kind in due.worker.KINDS:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.add(models.PanelSettings(id=1, ha_enabled=False))

        def operation(label, state="prepared", kind="purchase", retry=None, deadline=future, approval=False):
            row = mp.ProvisioningOperation(operation_type=kind, business_key=label, request_hash="b" * 64,
                tenant_scope_key="global", actor_kind="system", intent="SECRET-INTENT-NEVER-RETURNED",
                state=state, forward_deadline=deadline, wallet_epoch_at_start=0, next_retry_at=retry,
                completed_at=now if state in ("completed", "compensated") else None,
                approval_uuid=str(uuid.uuid4()) if approval else None,
                approval_execution_version=1 if approval else None, approval_key_bound=False if approval else None)
            db.add(row)
            db.flush()
            ids[label] = row.id
            return row

        def step(row, order=0, state="staged", retry=None, backend="mikrotik_wg"):
            db.add(mp.ProvisioningStep(operation_id=row.id, slot_key=str(order), step_order=order,
                direction="create", backend=backend, node_id=1,
                protocol="wireguard" if backend == "mikrotik_wg" else "pptp", state=state,
                remote_attempted=state not in ("staged", "removed") and backend != "radius_ppp",
                next_retry_at=retry, staged_wg_private_key="SECRET-KEY" if state == "staged" else None))

        step(operation("ready"))
        step(operation("op_wait", retry=future))
        step(operation("step_wait"), retry=future)
        step(operation("expired", retry=future, deadline=old), retry=future)
        operation("final", "remote_complete")
        operation("completed", "completed")
        operation("compensated", "compensated")
        operation("manual", "cleanup_required")
        operation("approval", approval=True)
        operation("delete", kind="delete_user")
        operation("renew", kind="renew_user")
        operation("live")
        operation("expired_lease")
        operation("unowned_lease")
        operation("null_deadline_lease")
        uncertain = operation("uncertain_first", "provisioning")
        step(uncertain, 0)  # Due staged slot must NOT bypass uncertain slot.
        step(uncertain, 1, "remote_calling", future)
        ordered = operation("slot_order", "provisioning")
        step(ordered, 0, retry=future)
        step(ordered, 1)
        comp = operation("comp_wait", "compensating", deadline=old)
        step(comp, state="compensating", retry=future)
        comp_order = operation("comp_order", "compensating")
        step(comp_order, 0, "compensating", future)
        step(comp_order, 1, "compensating")
        operation("comp_op_wait", "compensating", retry=future, deadline=old)
        operation("comp_final", "compensating", deadline=old)
        ppp = operation("ppp", kind="add_connection")
        step(ppp, backend="radius_ppp", retry=future)  # No remote PPP call.
        operation("create", kind="create_user")
        for label, owner, deadline in (("live", "op:900:1", future),
                ("expired_lease", "op:901:1", old), ("unowned_lease", None, future),
                ("null_deadline_lease", "op:902:1", None)):
            db.execute(rv.resource_locks.insert().values(resource_key=f"provisioning_op:{ids[label]}",
                lease_owner=owner, leased_until=deadline, fencing_epoch=7))
        db.commit()

    statements = []
    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", record)
    try:
        selected = due.select_due(Factory, host, limit=25, **expected)
        wanted = tuple(ids[label] for label in ("ready", "expired", "final", "expired_lease",
            "unowned_lease", "comp_final", "ppp", "create"))
        assert selected == wanted, (engine.dialect.name, selected, wanted)
        assert engine.pool.checkedout() == 0, "selection retained DB session"
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements), statements
        # The local SQLite may be newer than Linux's. Structural regression
        # check catches an outer-column ORDER BY even when it works locally.
        candidate_sql = next(sql for sql in statements if sql.startswith("SELECT provisioning_operations.id"))
        for ordering in re.findall(r"ORDER BY (.*?)\s+LIMIT", candidate_sql, re.DOTALL):
            assert "provisioning_operations." not in ordering or ordering == "provisioning_operations.id", ordering
        assert "SECRET" not in repr(selected)
        assert due.select_due(Factory, host, limit=2, **expected) == wanted[:2]
        assert due.select_due(Factory, host, limit=2, after_id=wanted[1], **expected) == wanted[2:4]
        assert due.select_due(Factory, host, after_id=max(ids.values()), **expected) == ()
        # Candidate is NOT claimed. Another worker may acquire it afterwards;
        # subsequent selection must not offer that now-live operation.
        with Factory() as db:
            resource_leases.begin_business(db)
            token = resource_leases.acquire(db, f"provisioning_op:{ids['ready']}", "op:990:1")
            db.commit()
        assert ids["ready"] not in due.select_due(Factory, host, limit=25, **expected)
        with Factory() as db:
            assert resource_leases.release(db, token)
            db.commit()
        for override in (dict(limit=True), dict(limit=0), dict(limit=26), dict(after_id=-1),
                dict(after_id=True), dict(ownership_epoch=0), dict(identity=None)):
            kwargs = dict(identity=host, **expected) | override
            before = len(statements)
            try:
                due.select_due(Factory, **kwargs)
                raise AssertionError("invalid selector argument accepted")
            except HTTPException as error:
                assert error.status_code == 422
            assert len(statements) == before
        try:
            due.select_due(Factory, host, **(expected | dict(ownership_epoch=2)))
            raise AssertionError("stale owner accepted")
        except HTTPException as error:
            assert error.detail == "provisioning_worker_owner_changed"
        # Reject changed safety state even on a cursor with no candidates.
        with Factory() as db:
            db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "shadow"
            db.commit()
        try:
            due.select_due(Factory, host, after_id=1000000, **expected)
            raise AssertionError("unsafe mode accepted on empty page")
        except HTTPException as error:
            assert error.detail == "provisioning_worker_owner_changed"
        with Factory() as db:
            runtime = db.get(mp.ProvisioningRuntimeState, 1)
            runtime.gate_mode, runtime.owner_state = "enforced", "draining"
            db.commit()
        assert due.select_due(Factory, host, limit=25, **expected) == wanted
        with Factory() as db:
            db.execute(rv.wallet_runtime_state.update().values(phase="fencing"))
            db.commit()
        try:
            due.select_due(Factory, host, **expected)
            raise AssertionError("wallet fencing accepted")
        except HTTPException as error:
            assert error.detail == "wallet_legacy_writer_unavailable"
        with Factory() as db:
            db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
            db.commit()
        try:
            due.select_due(Factory, HostIdentity("c" * 64, host.boot_id), **expected)
            raise AssertionError("foreign host accepted")
        except HTTPException as error:
            assert error.detail == "provisioning_worker_owner_changed"
        with Factory() as db:
            db.get(models.PanelSettings, 1).ha_enabled = True
            db.commit()
        try:
            due.select_due(Factory, host, **expected)
            raise AssertionError("HA accepted")
        except HTTPException as error:
            assert error.detail == "provisioning_ha_unsupported"
        with Factory() as db:
            db.get(models.PanelSettings, 1).ha_enabled = False
            db.get(mp.ProvisioningTypeMode, "purchase").mode = "legacy"
            db.commit()
        assert due.select_due(Factory, host, limit=25, **expected) == (ids["ppp"], ids["create"])
        with Factory() as db:
            for kind in due.worker.KINDS:
                db.get(mp.ProvisioningTypeMode, kind).mode = "legacy"
            db.commit()
        assert due.select_due(Factory, host, **expected) == ()
        assert _no_network.attempts == []
        print("PASS", engine.dialect.name, "bounded read-only selection, ordering, deadlines, leases, ownership and readiness")
    finally:
        event.remove(engine, "before_cursor_execute", record)


with tempfile.TemporaryDirectory(prefix="um-due-") as directory:
    local = create_engine("sqlite:///" + directory + "/due.db")
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
