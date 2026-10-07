"""Explicit inert-only namespace publication, crash faults and no overwrite."""
import datetime as dt
import os
import re
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models_provisioning as mp
from app.services import gate_locks, provisioning_schema as schema, provisioning_installation as installation
from app.services.provisioning_lock_verification import VerificationFailed


def refused(code, callback):
    try:
        callback()
        raise AssertionError("setup unexpectedly accepted")
    except installation.InstallationUnavailable as exc:
        assert str(exc) == code, (str(exc), code)


def scenario(engine, directory):
    Factory = sessionmaker(bind=engine)
    assert schema.bootstrap(engine, Factory)["ready"]
    with engine.connect() as connection:
        before = dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one())
    identity = before["installation_uuid"]
    target = Path(directory) / "installation.id"
    hold = gate_locks.FileLock(gate_locks.mode_lock_path(identity, directory)).acquire(shared=False, timeout=0)
    setup = lambda: installation.initialize_off(engine, identity, hold, base_dir=directory)
    try:
        with patch.object(schema, "inspect_schema", return_value=["injected schema failure"]):
            refused("installation_setup_schema_unverified", setup)
        assert not target.exists()
        with patch.object(installation.os, "link", side_effect=OSError("injected publication failure")):
            refused("installation_file_write_failed", setup)
        assert not target.exists() and not list(Path(directory).glob(".installation-*.tmp"))
        barrier = threading.Barrier(2)
        original_link = installation.os.link
        def concurrent_link(*args, **kwargs):
            barrier.wait(timeout=10)
            return original_link(*args, **kwargs)
        outcomes, errors = [], []
        def competitor():
            try:
                outcomes.append(setup()["created"])
            except Exception as exc:
                errors.append(type(exc).__name__)
        with patch.object(installation.os, "link", side_effect=concurrent_link):
            threads = [threading.Thread(target=competitor) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
        assert not errors and sorted(outcomes) == [False, True] and not any(t.is_alive() for t in threads)
        assert installation.require_match(engine, base_dir=directory) == identity
        assert not list(Path(directory).glob(".installation-*.tmp"))
        target.unlink()
        original_fsync = installation.os.fsync
        calls = []
        def fail_after_publication(descriptor):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("injected directory fsync failure")
            return original_fsync(descriptor)
        with patch.object(installation.os, "fsync", side_effect=fail_after_publication):
            refused("installation_file_write_failed", setup)
        # A complete published file is never removed by error cleanup.
        assert installation.require_match(engine, base_dir=directory) == identity
        assert not list(Path(directory).glob(".installation-*.tmp"))
        inode = target.stat().st_ino
        with patch.object(installation.os, "fsync", wraps=original_fsync) as sync:
            assert setup() == dict(created=False, installation_uuid=identity)
            assert sync.call_count == 1  # Retry confirms directory durability.
        assert target.stat().st_ino == inode
        target.unlink()
        statements = []
        def collect(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(engine, "before_cursor_execute", collect)
        try:
            assert setup() == dict(created=True, installation_uuid=identity)
            assert setup() == dict(created=False, installation_uuid=identity)
        finally:
            event.remove(engine, "before_cursor_execute", collect)
        assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|REPLACE|TRUNCATE)\b", sql, re.I)
                       for sql in statements)
        assert target.read_text() == identity + "\n" and target.stat().st_mode & 0o777 == 0o600
        for raw, code in ((str(uuid.uuid4()), "installation_mismatch"), ("broken", "installation_file_invalid")):
            target.write_text(raw)
            inode = target.stat().st_ino
            refused(code, setup)
            assert target.read_text() == raw and target.stat().st_ino == inode
        target.unlink()
        db = Factory()
        try:
            runtime = db.get(mp.ProvisioningRuntimeState, 1)
            runtime.lock_backend = "flock"
            runtime.lock_verified_at = dt.datetime.utcnow()
            runtime.gate_mode = "shadow"
            db.commit()
            refused("installation_setup_not_inert", setup)
            assert not target.exists()
            runtime.gate_mode = "off"
            runtime.lock_backend = "none"
            runtime.lock_verified_at = None
            db.get(mp.ProvisioningTypeMode, "create_user").mode = "durable"
            db.commit()
            refused("installation_setup_not_inert", setup)
            db.get(mp.ProvisioningTypeMode, "create_user").mode = "legacy"
            operation = mp.ProvisioningOperation(operation_type="create_user", business_key="namespace-pending",
                request_hash="a" * 64, tenant_scope_key="global", actor_kind="system", intent="{}",
                state="prepared", forward_deadline=dt.datetime.utcnow(), wallet_epoch_at_start=0)
            db.add(operation)
            db.commit()
            refused("installation_setup_not_inert", setup)
            assert db.get(mp.ProvisioningOperation, operation.id).state == "prepared" and not target.exists()
            db.delete(operation)
            db.commit()
        finally:
            db.close()
        assert setup()["created"]
        with engine.connect() as connection:
            assert dict(connection.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()) == before
    finally:
        hold.release()
    try:
        setup()
        raise AssertionError("released mode hold accepted")
    except VerificationFailed:
        pass
    print("PASS", engine.dialect.name, "inert-only atomic initialization, pre/post publication failure, immutable retry, no DB writes")


with tempfile.TemporaryDirectory(prefix="um-initialize-") as directory:
    local = Path(directory) / "local"
    local.mkdir()
    engine = create_engine("sqlite:///" + str(Path(directory) / "test.db"))
    try:
        scenario(engine, str(local))
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        remote = Path(directory) / "remote"
        remote.mkdir()
        engine = scratch.claim(url)
        try:
            scenario(engine, str(remote))
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
