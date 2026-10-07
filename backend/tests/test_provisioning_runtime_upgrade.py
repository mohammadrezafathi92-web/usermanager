"""Explicit CHECK upgrade: preserve every value, SQLite rollback, Maria retry."""
import datetime as dt
import os
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import CheckConstraint, create_engine, inspect, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import provisioning_schema as schema, receipt_void_schema, gate_locks
from app.services import provisioning_runtime_upgrade as migration, provisioning_runtime_contract as contract
from app.services import provisioning_ownership as owner, provisioning_lock_verification as verification, resource_leases
from app.services.provisioning_host import HostIdentity, OwnershipMismatch, validate_snapshot


def scenario(engine, lock_dir):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    user = models.User(username="survivor", balance=1234, used_bytes=5678, total_quota_bytes=9999)
    db.add_all([root, user])
    db.commit()
    aid, uid = root.id, user.id
    installation = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    db.rollback()
    before = None
    with engine.connect() as connection:
        before = dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one())
        assert not contract.supports_release(connection)
    hold = gate_locks.FileLock(gate_locks.mode_lock_path(installation, lock_dir)).acquire(shared=False, timeout=0)
    namespace = Path(lock_dir) / "installation.id"
    namespace.write_text(installation)
    namespace.chmod(0o600)
    arguments = dict(actor_admin_id=aid, expected_version=0, base_dir=lock_dir)
    try:
        namespace.write_text(str(uuid.uuid4()))
        try:
            migration.upgrade(engine, installation, hold, **arguments)
            raise AssertionError("mismatched file accepted for a schema upgrade")
        except migration.UpgradeRefused as exc:
            assert str(exc) == "installation_mismatch"
        namespace.write_text(installation)
        # Unknown restrictive CHECKs must not become accepted by compatibility.
        # Test an unrelated extra index: no artifact is silently discarded.
        with engine.begin() as connection:
            connection.execute(text("CREATE INDEX runtime_extra ON provisioning_runtime_state(version)"))
        try:
            migration.upgrade(engine, installation, hold, **arguments)
            raise AssertionError("extra index discarded")
        except migration.UpgradeRefused as exc:
            assert str(exc) == "upgrade_unknown_artifacts"
        with engine.begin() as connection:
            suffix = " ON provisioning_runtime_state" if engine.dialect.name != "sqlite" else ""
            connection.execute(text("DROP INDEX runtime_extra" + suffix))
        if engine.dialect.name == "sqlite":
            phases = ("after_copy", "after_drop", "after_ddl")
        else:
            # MariaDB DDL is crash-atomic but implicitly commits. Do NOT claim
            # that a user transaction can undo a successfully executed ALTER.
            phases = ("before_ddl",)
        for phase in phases:
            def crash(current):
                if current == phase:
                    raise RuntimeError("injected upgrade crash")
            try:
                with patch.object(migration, "_checkpoint", side_effect=crash):
                    migration.upgrade(engine, installation, hold, **arguments)
                raise AssertionError("injection failed")
            except RuntimeError as exc:
                assert str(exc) == "injected upgrade crash"
            with engine.connect() as connection:
                assert dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()) == before
                assert not contract.supports_release(connection)
                assert migration.TEMP not in inspect(connection).get_table_names()
                assert not schema.inspect_schema(connection)
            assert db.get(models.User, uid).balance == 1234
            db.rollback()
        if engine.dialect.name != "sqlite":
            def crash_after_alter(phase):
                if phase == "after_ddl":
                    raise RuntimeError("post-ALTER observer failure")
            try:
                with patch.object(migration, "_checkpoint", side_effect=crash_after_alter):
                    migration.upgrade(engine, installation, hold, **arguments)
                raise AssertionError("post-DDL injection failed")
            except RuntimeError as exc:
                assert str(exc) == "post-ALTER observer failure"
            with engine.connect() as connection:
                assert contract.supports_release(connection)
                assert dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()) == before
        result = migration.upgrade(engine, installation, hold, **arguments)
        assert result == dict(changed=engine.dialect.name == "sqlite", version=2)
        assert migration.upgrade(engine, installation, hold, **arguments) == dict(changed=False, version=2)
        with engine.connect() as connection:
            assert dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()) == before
            assert not schema.inspect_schema(connection)
            assert not schema.inspect_schema(connection, (migration.runtime_table(contract.RELEASED),))
            assert schema.inspect_schema(connection, (migration.runtime_table(contract.LEGACY),))
        # Startup recognizes ONLY these two exact CHECK versions, and still
        # requires inert fixed-row contents. No new mode is seeded/activated.
        assert schema.bootstrap(engine, Factory)["ready"]
        assert db.get(models.User, uid).balance == 1234 and db.get(models.User, uid).used_bytes == 5678
        assert all(row.mode == "legacy" for row in db.query(mp.ProvisioningTypeMode))
        db.rollback()
        # Real enforced handoff after upgrade: never lower the gate. A vacant
        # owner fails host revalidation until a new verified claim succeeds.
        identity = HostIdentity("a" * 64, str(uuid.uuid4()))
        if sys.platform == "linux":
            proof = verification.verify(engine, identity, installation, hold, base_dir=lock_dir)
        else:
            proof = verification.VerifiedLocks(identity, installation,
                "flock" if engine.dialect.name == "sqlite" else "flock+get_lock", dt.datetime.utcnow())
        arguments = dict(actor_admin_id=aid, base_dir=lock_dir)
        resource_leases.begin_business(db)
        owner.claim(db, proof, 0, hold, **arguments)
        db.commit()
        db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "enforced"  # Fixture only.
        db.commit()
        resource_leases.begin_business(db)
        owner.drain(db, proof, 1, hold, **arguments)
        db.commit()
        pending = mp.ProvisioningOperation(operation_type="purchase", business_key=str(uuid.uuid4()),
            request_hash="0" * 64, tenant_scope_key="shared", actor_kind="system", intent="{}", state="prepared",
            target_user_id=uid, wallet_epoch_at_start=0, forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
        db.add(pending)
        db.commit()
        resource_leases.begin_business(db)
        try:
            owner.release(db, proof, 2, hold, {}, **arguments)
            raise AssertionError("pending operation ignored")
        except HTTPException as exc:
            assert exc.detail["code"] == "ownership_operations_pending"
        db.rollback()
        db.delete(pending)  # Disposable fixture only, not a production deletion path.
        node = models.Node(name="release-lock", type=models.NodeType.mikrotik)
        db.add(node)
        db.commit()
        nid = node.id
        db.rollback()
        resource_leases.begin_business(db)
        try:
            owner.release(db, proof, 2, hold, {}, **arguments)
            raise AssertionError("missing node hold accepted")
        except HTTPException as exc:
            assert exc.detail == "ownership_nodes_not_quiescent"
        db.rollback()
        node_hold = gate_locks.FileLock(gate_locks.node_lock_path(installation, nid, lock_dir)).acquire(shared=False, timeout=0)
        resource_leases.begin_business(db)
        try:
            owner.release(db, proof, 2, hold, {nid: node_hold}, **arguments)
            db.commit()
        finally:
            node_hold.release()
        row = db.get(mp.ProvisioningRuntimeState, 1)
        assert row.owner_state == "released" and row.owner_host_id is None and row.gate_mode == "enforced"
        snapshot = db.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()
        try:
            validate_snapshot(snapshot, identity, 1, installation)
            raise AssertionError("vacant enforced owner accepted")
        except OwnershipMismatch:
            pass
        db.rollback()
        resource_leases.begin_business(db)
        owner.claim(db, proof, 3, hold, **arguments)
        db.commit()
        row = db.get(mp.ProvisioningRuntimeState, 1)
        assert row.owner_state == "active" and row.ownership_epoch == 3 and row.gate_mode == "enforced"
        db.rollback()
        assert not schema.inspect_schema(engine)
        print("PASS", engine.dialect.name, "controlled CHECK upgrade, preserved data, recovery and enforced handoff")
    finally:
        hold.release()
        db.close()


def adversarial(engine):
    schema.create_tables(engine)
    for expression in ("1 = 1", "gate_mode <> 'enforced' OR owner_state IN ('active','draining','released','none')"):
        mp.ProvisioningRuntimeState.__table__.drop(engine)
        migration.runtime_table(expression).create(engine)
        problems = schema.inspect_schema(engine)
        assert any(contract.NAME in problem and "different expression" in problem for problem in problems), problems
        Factory = sessionmaker(bind=engine)
        assert not schema.bootstrap(engine, Factory)["ready"]
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM provisioning_runtime_state")).scalar_one() == 0
    mp.ProvisioningRuntimeState.__table__.drop(engine)
    table = migration.runtime_table(contract.RELEASED)
    table.append_constraint(CheckConstraint("version < 100", name="ck_unexpected"))
    table.create(engine)
    assert any("unexpected check ck_unexpected" in problem for problem in schema.inspect_schema(engine))
    print("PASS", engine.dialect.name, "only the exact v1/v2 expressions accepted; arbitrary wider CHECKs refused")


with tempfile.TemporaryDirectory(prefix="um-runtime-upgrade-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine, os.path.join(directory, "locks"))
    finally:
        engine.dispose()
    engine = create_engine("sqlite:///" + os.path.join(directory, "adversarial.db"))
    try:
        adversarial(engine)
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        engine = scratch.claim(url)
        try:
            scenario(engine, os.path.join(directory, "maria-locks"))
        finally:
            scratch.release(engine)
        engine = scratch.claim(url)
        try:
            adversarial(engine)
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
