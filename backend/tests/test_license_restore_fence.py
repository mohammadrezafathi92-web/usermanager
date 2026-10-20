"""Real SQL sessions and thread ordering; node mutations are fake only."""
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import license_fence, license_guard, licensing, quota_manager as qm
from app.services import provisioning_schema, receipt_void_schema

checks = 0


def check(label, result):
    global checks
    assert result, label
    checks += 1
    print("PASS", label)


def scenario(engine, folder):
    os.environ["UM_LOCK_DIR"] = str(Path(folder) / "locks")
    models.Base.metadata.create_all(engine)
    mp.ProvisioningOperation.__table__.create(engine)
    license_fence.install(engine)
    Session = sessionmaker(bind=engine)
    with Session() as db:
        user = models.User(username="fence", status=models.UserStatus.active,
                           total_quota_bytes=1000, used_bytes=1)
        node = models.Node(name="fake", type=models.NodeType.mikrotik, enabled=True)
        db.add_all([user, node]); db.flush()
        c = models.Connection(user_id=user.id, node_id=node.id, type=models.ConnectionType.wireguard,
                              wg_peer_name="fake", enabled=True, license_suspended=True, license_cut_applied=True)
        db.add(c); db.commit()
        cid, uid = c.id, user.id
        db.refresh(c)
        c.license_target_hash = qm._license_target_hash(qm._license_connection(db, cid))
        db.commit()

    calls, errors = [], []
    entered, release, writer_started, writer_done = (threading.Event() for _ in range(4))

    def fake_remote(conn, enabled):
        calls.append(enabled)
        if enabled:
            entered.set()
            if not release.wait(4):
                raise AssertionError("restore test not released")
        return True

    def restore():
        try:
            qm.restore_license_connection(cid)
        except Exception as exc:
            errors.append(exc)

    def disable():
        try:
            writer_started.set()
            with Session() as db:
                db.get(models.Connection, cid).enabled = False
                db.commit()
            writer_done.set()
        except Exception as exc:
            errors.append(exc)

    with patch.object(qm, "SessionLocal", Session), patch.object(license_guard, "current", return_value=licensing.Enforcement()), \
            patch.object(provisioning_schema, "is_ready", return_value=True), \
            patch.object(receipt_void_schema, "is_ready", return_value=True), \
            patch.object(qm, "_license_remote_state", side_effect=fake_remote):
        first = threading.Thread(target=restore)
        first.start()
        check("remote restoration entered", entered.wait(3))
        second = threading.Thread(target=disable)
        second.start()
        check("ordinary writer started", writer_started.wait(1))
        check("ordinary transaction cannot cross restoration", not writer_done.wait(.15))
        release.set()
        first.join(5); second.join(5)
        check("threads finished without errors", not first.is_alive() and not second.is_alive() and not errors)
        with Session() as db:
            c = db.get(models.Connection, cid)
            check("later administrator disable wins", c.enabled is False and c.license_suspended is False)

        # Opposite order: a transaction that already read the account must
        # finish its decision before the exclusive restore can read it.
        with Session() as db:
            c = db.get(models.Connection, cid)
            c.enabled = True; c.license_suspended = True; c.license_cut_applied = True
            c.license_target_hash = qm._license_target_hash(qm._license_connection(db, cid))
            db.commit()
        calls.clear(); errors.clear()
        reader_open, commit_reader = threading.Event(), threading.Event()

        def old_transaction():
            try:
                with Session() as db:
                    u = db.get(models.User, uid)
                    reader_open.set()
                    if not commit_reader.wait(4):
                        raise AssertionError("writer test not released")
                    u.status = models.UserStatus.disabled
                    db.commit()
            except Exception as exc:
                errors.append(exc)

        old = threading.Thread(target=old_transaction)
        old.start(); check("existing transaction opened", reader_open.wait(2))
        entered.clear()
        attempt = threading.Thread(target=restore)
        attempt.start()
        check("old transaction prevents remote enable", not entered.wait(.15) and not calls)
        commit_reader.set(); old.join(5); attempt.join(5)
        check("disabled account never remotely enabled", not calls)
        check("restore rejected after prior decision", len(errors) == 1 and getattr(errors[0], "detail", None) == "license_restore_not_eligible")

        # Identity replacement while suspended must not inherit a cut marker.
        with Session() as db:
            db.get(models.User, uid).status = models.UserStatus.active
            db.get(models.Connection, cid).wg_peer_name = "different-credential"
            db.commit()
        errors.clear(); restore()
        check("changed target cannot inherit restoration", len(errors) == 1 and getattr(errors[0], "detail", None) == "license_restore_target_changed")
        check("changed target never enabled", not calls)


with tempfile.TemporaryDirectory(prefix="license-fence-") as temp:
    engine = create_engine("sqlite:///" + str(Path(temp) / "test.db"), connect_args={"check_same_thread": False})
    try:
        scenario(engine, temp)
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        engine = scratch.claim(url)
        try:
            scenario(engine, temp)
        finally:
            scratch.release(engine)
check("no unapproved network", not _no_network.attempts)
print(f"all {checks} checks passed")
