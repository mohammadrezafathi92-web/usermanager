"""The pinned updater may hold exactly one unknown Xray deletion safely."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

os.environ["SECRET_KEY"] = "safe-update-test-only-not-a-production-secret"

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
spec = importlib.util.spec_from_file_location("update_preflight_stalled", ROOT / "backend/scripts/update_preflight.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)

with tempfile.TemporaryDirectory(prefix="um-update-stalled-xray-") as directory:
    engine = create_engine("sqlite:///" + str(Path(directory) / "panel.db"))
    with engine.begin() as db:
        db.execute(text("""
            CREATE TABLE provisioning_operations (
                id INTEGER PRIMARY KEY, operation_type TEXT NOT NULL, state TEXT NOT NULL
            )
        """))
        db.execute(text("""
            CREATE TABLE provisioning_steps (
                id INTEGER PRIMARY KEY, operation_id INTEGER NOT NULL, direction TEXT NOT NULL,
                backend TEXT NOT NULL, protocol TEXT NOT NULL, state TEXT NOT NULL,
                error_code TEXT, remote_outcome TEXT, remote_attempted INTEGER NOT NULL,
                xr_email TEXT, xr_panel_inbound_id INTEGER, step_order INTEGER NOT NULL
            )
        """))
        db.execute(text("""
            CREATE TABLE resource_locks (
                resource_key TEXT PRIMARY KEY, lease_owner TEXT, fencing_epoch INTEGER NOT NULL,
                leased_until TEXT, heartbeat_at TEXT, version INTEGER NOT NULL
            )
        """))
        db.execute(text("INSERT INTO provisioning_operations VALUES (182,'delete_purchase','provisioning')"))
        db.execute(text("""
            INSERT INTO provisioning_steps VALUES
                (470,182,'remove','threexui','xray','remote_calling','remote_result_unknown',NULL,1,
                 'known-user@example.invalid',1,0),
                (471,182,'remove','mikrotik_wg','wireguard','staged',NULL,NULL,0,NULL,NULL,1),
                (472,182,'remove','radius_ppp','openvpn','staged',NULL,NULL,0,NULL,NULL,2)
        """))
        db.execute(text("INSERT INTO resource_locks VALUES ('provisioning_op:182',NULL,0,NULL,NULL,0)"))

    hold = helper._acquire_operation_hold(engine, 182)
    assert hold["operation_id"] == 182 and hold["fencing_epoch"] == 1
    with engine.connect() as db:
        helper._validate_operation_hold(db, hold)
    snapshot = helper.runtime_snapshot(engine, operation_hold=hold)
    assert snapshot["provisioning_runtime_state"] is None

    try:
        helper._acquire_operation_hold(engine, 182)
        raise AssertionError("a second deployment must not steal the live operation hold")
    except RuntimeError as exc:
        assert "busy" in str(exc)

    with engine.begin() as db:
        db.execute(text("INSERT INTO provisioning_operations VALUES (183,'purchase','provisioning')"))
    try:
        helper.runtime_snapshot(engine, operation_hold=hold)
        raise AssertionError("unrelated unfinished operations must still block updates")
    except RuntimeError as exc:
        assert "unfinished durable operations" in str(exc)
    with engine.begin() as db:
        db.execute(text("DELETE FROM provisioning_operations WHERE id=183"))

    helper._release_operation_hold(engine, hold)
    try:
        helper.runtime_snapshot(engine, operation_hold=hold)
        raise AssertionError("snapshot must reject a released hold")
    except RuntimeError as exc:
        assert "not live" in str(exc)

    # Exercise the real backup path: it should hold the operation lease,
    # create an integrity-checked DB snapshot, and leave release to updater.
    bot_db = Path(directory) / "bot.db"
    bot_engine = create_engine("sqlite:///" + str(bot_db))
    with bot_engine.begin() as db:
        db.execute(text("CREATE TABLE backup_proof (value TEXT)"))
        db.execute(text("INSERT INTO backup_proof VALUES ('kept')"))
    bot_engine.dispose()
    import app.config
    panel_url = "sqlite:///" + str(Path(directory) / "panel.db")
    with patch.object(app.config.settings, "database_url", panel_url), patch.object(
        helper, "configured_engine", side_effect=lambda _: create_engine(panel_url)
    ), patch.dict(os.environ, {
        "BACKUP_DIR": str(Path(directory) / "backups"),
        "BOT_DB_PATH": str(bot_db),
        "UM_UPDATE_STALLED_XRAY_DELETE_ID": "182",
    }):
        manifest = helper.create_backup("update_stalled_xray")
        assert manifest["operation_hold"]["operation_id"] == 182
        backup = Path(manifest["directory"])
        helper.verify_backup(backup)
        with create_engine("sqlite:///" + str(backup / "bot.db")).connect() as db:
            assert db.execute(text("SELECT value FROM backup_proof")).scalar_one() == "kept"
        with engine.connect() as db:
            helper._validate_operation_hold(db, manifest["operation_hold"])
        helper._release_operation_hold(engine, manifest["operation_hold"])

    with engine.begin() as db:
        db.execute(text("UPDATE provisioning_steps SET protocol='openvpn' WHERE id=470"))
    try:
        helper._acquire_operation_hold(engine, 182)
        raise AssertionError("non-Xray unresolved steps must not be eligible")
    except RuntimeError as exc:
        assert "approved Xray delete" in str(exc)

    engine.dispose()

print("safe updater stalled Xray lease: exact-state, fencing, rejection and release PASS")
