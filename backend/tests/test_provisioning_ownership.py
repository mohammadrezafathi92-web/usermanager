"""Explicit owner CAS/audit rollback; real Linux flock/death and MariaDB locks."""
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import OperationalError
from app import models, models_provisioning as mp
from app.services import provisioning_schema, receipt_void_schema, provisioning_ownership as owner
from app.services import provisioning_lock_verification as verification, gate_locks, resource_leases
from app.services.provisioning_host import HostIdentity, OwnershipMismatch, validate_snapshot


def refused(code, callback):
    try:
        callback()
        raise AssertionError("unexpected success")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


def scenario(engine, base_dir):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    other = models.AdminUser(username="other", hashed_password="x", is_superadmin=False)
    db.add_all([root, other])
    db.commit()
    root_id, other_id = root.id, other.id
    installation = db.get(mp.ProvisioningRuntimeState, 1).installation_uuid
    db.rollback()
    identity = HostIdentity("a" * 64, str(uuid.uuid4()))
    hold = gate_locks.FileLock(gate_locks.mode_lock_path(installation, base_dir)).acquire(shared=False, timeout=0)
    try:
        if sys.platform == "linux":
            proof = verification.verify(engine, identity, installation, hold, base_dir=base_dir)
            assert proof.backend == ("flock" if engine.dialect.name == "sqlite" else "flock+get_lock")
            print("PASS", engine.dialect.name, "real local descriptor/process-death/advisory self-tests")
        else:
            # CAS unit fixture only. The real verification MUST refuse macOS;
            # no production proof is fabricated by an unsupported platform.
            try:
                verification.verify(engine, identity, installation, hold, base_dir=base_dir)
                raise AssertionError("unsupported platform accepted")
            except verification.VerificationFailed as exc:
                assert str(exc) == "lock_verification_requires_linux"
            proof = verification.VerifiedLocks(identity, installation,
                "flock" if engine.dialect.name == "sqlite" else "flock+get_lock", dt.datetime.utcnow())
            print("SKIP real Linux lock verification locally; CAS uses an explicit unit fixture")
        arguments = dict(actor_admin_id=root_id, base_dir=base_dir)
        commits = []
        event.listen(db, "after_commit", lambda session: commits.append(True))
        resource_leases.begin_business(db)
        refused("ownership_superadmin_required", lambda: owner.claim(db, proof, 0, hold,
            actor_admin_id=other_id, base_dir=base_dir))
        db.rollback()
        resource_leases.begin_business(db)
        refused("ownership_lock_not_ready", lambda: owner.claim(db,
            replace(proof, checked_at=dt.datetime.utcnow() - dt.timedelta(minutes=10)), 0, hold, **arguments))
        db.rollback()
        resource_leases.begin_business(db)
        refused("ownership_held", lambda: owner.claim(db, proof, 1, hold, **arguments))
        db.rollback()

        def crash_audit(session, context, instances):
            if any(isinstance(row, mp.ProvisioningOwnershipEvent) for row in session.new):
                raise RuntimeError("injected audit failure after owner CAS")

        event.listen(db, "before_flush", crash_audit)
        resource_leases.begin_business(db)
        try:
            owner.claim(db, proof, 0, hold, **arguments)
            raise AssertionError("injected crash missing")
        except RuntimeError as exc:
            assert str(exc) == "injected audit failure after owner CAS"
        db.rollback()
        event.remove(db, "before_flush", crash_audit)
        assert db.get(mp.ProvisioningRuntimeState, 1).owner_state == "none"
        assert db.query(mp.ProvisioningOwnershipEvent).count() == 0 and not commits
        db.rollback()
        resource_leases.begin_business(db)
        row = owner.claim(db, proof, 0, hold, **arguments)
        assert row.owner_host_id == identity.host_id and row.owner_boot_id == identity.boot_id
        assert row.ownership_epoch == 1 and row.version == 1 and row.gate_mode == "off"
        assert not commits
        db.commit()
        assert len(commits) == 1
        row = db.get(mp.ProvisioningRuntimeState, 1)
        original_claimed = row.owner_claimed_at
        assert [row.event_type for row in db.query(mp.ProvisioningOwnershipEvent)] == ["claim"]
        db.rollback()
        resource_leases.begin_business(db)
        row = owner.claim(db, proof, 1, hold, **arguments)
        assert row.ownership_epoch == 1 and row.owner_claimed_at == original_claimed and row.version == 2
        db.commit()
        assert db.query(mp.ProvisioningOwnershipEvent).count() == 2
        # A very old heartbeat can NEVER authorize another host or a reboot.
        db.get(mp.ProvisioningRuntimeState, 1).owner_heartbeat_at = dt.datetime(2000, 1, 1)
        db.commit()
        stranger = replace(proof, identity=HostIdentity("b" * 64, str(uuid.uuid4())))
        reboot = replace(proof, identity=HostIdentity(identity.host_id, str(uuid.uuid4())))
        for candidate in (stranger, reboot):
            resource_leases.begin_business(db)
            refused("ownership_held", lambda: owner.claim(db, candidate, 2, hold, **arguments))
            db.rollback()
        resource_leases.begin_business(db)
        refused("ownership_reclaim_confirmation_required", lambda: owner.reclaim(db, reboot, 2, hold,
            reason="reboot", confirmation="", **arguments))
        db.rollback()
        resource_leases.begin_business(db)
        refused("ownership_reclaim_invalid", lambda: owner.reclaim(db, stranger, 2, hold,
            reason="other host", confirmation=owner.RECLAIM_CONFIRMATION, **arguments))
        db.rollback()
        resource_leases.begin_business(db)
        row = owner.reclaim(db, reboot, 2, hold, reason="explicit reboot recovery",
            confirmation=owner.RECLAIM_CONFIRMATION, **arguments)
        assert row.ownership_epoch == 2 and row.version == 3 and row.owner_boot_id == reboot.identity.boot_id
        db.commit()
        snapshot = db.execute(select(mp.ProvisioningRuntimeState.__table__)).mappings().one()
        try:
            validate_snapshot(snapshot, identity, 1, installation)
            raise AssertionError("old process unfenced")
        except OwnershipMismatch:
            pass
        validate_snapshot(snapshot, reboot.identity, 2, installation)
        assert [row.event_type for row in db.query(mp.ProvisioningOwnershipEvent).order_by(mp.ProvisioningOwnershipEvent.id)] == [
            "claim", "verify", "reclaim"]
        audit = json.loads(db.query(mp.ProvisioningOwnershipEvent).filter_by(event_type="reclaim").one().reason)
        assert audit == dict(reason="explicit reboot recovery", from_boot_id=identity.boot_id,
            to_boot_id=reboot.identity.boot_id)
        assert all(row.mode == "legacy" for row in db.query(mp.ProvisioningTypeMode))
        assert db.query(models.Connection).count() == 0
        db.rollback()
        resource_leases.begin_business(db)
        row = owner.drain(db, reboot, 3, hold, **arguments)
        assert row.owner_state == "draining" and row.ownership_epoch == 2 and row.version == 4
        db.commit()
        resource_leases.begin_business(db)
        refused("ownership_held", lambda: owner.claim(db, reboot, 4, hold, **arguments))
        db.rollback()
        resource_leases.begin_business(db)
        row = owner.cancel_drain(db, reboot, 4, hold, **arguments)
        assert row.owner_state == "active" and row.ownership_epoch == 2 and row.version == 5
        db.commit()
        resource_leases.begin_business(db)
        owner.drain(db, reboot, 5, hold, **arguments)
        db.commit()
        db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "enforced"
        db.commit()
        resource_leases.begin_business(db)
        refused("ownership_release_protocol_unavailable", lambda: owner.release(db, reboot, 6, hold, {}, **arguments))
        db.rollback()
        assert db.get(mp.ProvisioningRuntimeState, 1).owner_state == "draining"
        db.get(mp.ProvisioningRuntimeState, 1).gate_mode = "off"
        db.commit()
        resource_leases.begin_business(db)
        row = owner.release(db, reboot, 6, hold, {}, **arguments)
        assert row.owner_state == "released" and row.owner_host_id is None and row.ownership_epoch == 3
        db.commit()
        # Fixture reset only: exercise two genuine database sessions claiming
        # the same vacant version; the singleton CAS accepts exactly one.
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.owner_state = "released"
        row.owner_host_id = row.owner_boot_id = row.owner_claimed_at = row.owner_heartbeat_at = None
        db.commit()
        version = db.get(mp.ProvisioningRuntimeState, 1).version
        audit_count = db.query(mp.ProvisioningOwnershipEvent).count()
        db.rollback()
        barrier, outcomes, errors = threading.Barrier(2), [], []

        def competitor(candidate):
            session = Factory()
            try:
                barrier.wait(timeout=5)
                for attempt in range(3):
                    resource_leases.begin_business(session)
                    try:
                        row = owner.claim(session, candidate, version, hold, **arguments)
                        host = row.owner_host_id
                        session.commit()
                        outcomes.append(("claimed", host))
                        break
                    except HTTPException as exc:
                        session.rollback()
                        assert exc.detail == "ownership_held"
                        outcomes.append((exc.detail, candidate.identity.host_id))
                        break
                    except OperationalError as exc:
                        session.rollback()
                        if engine.dialect.name == "sqlite" or not exc.orig.args or exc.orig.args[0] not in (1020, 1205, 1213) or attempt == 2:
                            raise
                        # Retrying the entire writer transaction preserves the
                        # original expected version: the loser then gets 409.
                        time.sleep(0.02 * (attempt + 1))
            except Exception as exc:
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=competitor, args=(candidate,)) for candidate in (proof, stranger)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)
        assert all(not thread.is_alive() for thread in threads) and not errors, errors
        assert sorted(outcome[0] for outcome in outcomes) == ["claimed", "ownership_held"]
        row = db.get(mp.ProvisioningRuntimeState, 1)
        assert row.version == version + 1 and row.ownership_epoch == 4
        assert row.owner_host_id == next(host for result, host in outcomes if result == "claimed")
        assert db.query(mp.ProvisioningOwnershipEvent).count() == audit_count + 1
        db.rollback()
        hold.release()
        resource_leases.begin_business(db)
        try:
            owner.claim(db, reboot, version + 1, hold, **arguments)
            raise AssertionError("released exclusive lock accepted")
        except verification.VerificationFailed as exc:
            assert str(exc) == "gate_mode_exclusive_required"
        db.rollback()
        print("PASS", engine.dialect.name, "owner CAS, audit atomicity, no heartbeat takeover, explicit reboot fencing")
    finally:
        hold.release()
        db.close()


with tempfile.TemporaryDirectory(prefix="um-ownership-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine, os.path.join(directory, "locks"))
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        engine = scratch.claim(url)
        try:
            scenario(engine, os.path.join(directory, "maria-locks"))
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
