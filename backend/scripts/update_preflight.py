"""Update-only backup/runtime helper: no startup, Telegram or node calls.

Stream into the existing backend; host validation uses only Python stdlib.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from urllib.parse import quote

RUNTIME_FIELDS = {
    "provisioning_runtime_state": ("gate_mode", "gate_mode_epoch", "owner_state", "ownership_epoch", "lock_backend"),
    "wallet_runtime_state": ("phase", "epoch", "external_fencing"),
    "receipt_approval_runtime_state": ("registration_mode", "auto_rate_limit_mode", "loyalty_timing_mode", "completeness_generation", "key_identity_ready", "key_identity_failure"),
    "session_cap_runtime_state": ("mode", "epoch"),
    "panel_settings": ("ha_enabled", "receipt_void_preview_enabled", "receipt_void_execute_enabled", "receipt_void_worker_enabled"),
}
OPERATION_HOLD_TTL_SECONDS = 3600
DEPLOYMENT_HOLD_NONCE = 9223372036854775807
NONTERMINAL_PROVISIONING_STATES = ("completed", "compensated")
NONTERMINAL_WALLET_STATES = ("completed", "failed", "cancelled")


def sqlite_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise RuntimeError("configured database file missing")
    src = sqlite3.connect("file:" + quote(str(source.resolve())) + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        if dst.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite backup integrity failed")
    finally:
        dst.close()
        src.close()
    target.chmod(0o600)


def _database_now(dialect: str) -> str:
    if dialect == "sqlite":
        return "strftime('%Y-%m-%d %H:%M:%f','now')"
    if dialect in ("mysql", "mariadb"):
        return "NOW(6)"
    raise RuntimeError("unsupported database for operation hold")


def _stalled_xray_delete(connection, operation_id: int, *, lock: bool = False) -> None:
    """Allow only the one known, attempted 3X-UI Xray delete with unknown outcome."""
    from sqlalchemy import text

    suffix = " FOR UPDATE" if lock and connection.dialect.name in ("mysql", "mariadb") else ""
    open_operations = connection.execute(text(
        "SELECT id, operation_type, state FROM provisioning_operations "
        "WHERE state NOT IN ('completed','compensated') ORDER BY id" + suffix
    )).mappings().all()
    if len(open_operations) != 1 or (int(open_operations[0]["id"]) != operation_id or
            open_operations[0]["operation_type"] != "delete_purchase" or
            open_operations[0]["state"] != "provisioning"):
        raise RuntimeError("unfinished durable operations")

    steps = connection.execute(text(
        "SELECT id,direction,backend,protocol,state,error_code,remote_outcome,remote_attempted,"
        "xr_email,xr_panel_inbound_id FROM provisioning_steps WHERE operation_id=:operation_id "
        "ORDER BY step_order,id" + suffix
    ), {"operation_id": operation_id}).mappings().all()
    unknown = [row for row in steps if row["state"] == "remote_calling"]
    if len(unknown) != 1:
        raise RuntimeError("stalled operation is not the approved Xray delete")
    step = unknown[0]
    if (step["direction"], step["backend"], step["protocol"], step["error_code"],
            step["remote_outcome"], bool(step["remote_attempted"])) != (
            "remove", "threexui", "xray", "remote_result_unknown", None, True):
        raise RuntimeError("stalled operation is not the approved Xray delete")
    if not step["xr_email"] or step["xr_panel_inbound_id"] is None:
        raise RuntimeError("stalled Xray identity is incomplete")
    if any(row["state"] not in ("staged", "removed", "remote_calling") for row in steps):
        raise RuntimeError("stalled operation has an unsafe step state")


def _parse_operation_hold(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        operation_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("invalid stalled-operation id") from exc
    if str(operation_id) != raw or operation_id < 1:
        raise RuntimeError("invalid stalled-operation id")
    return {"operation_id": operation_id}


def _hold_from_environment() -> dict | None:
    raw_id = os.environ.get("UM_UPDATE_STALLED_XRAY_DELETE_ID", "")
    hold = _parse_operation_hold(raw_id)
    if hold is None:
        return None
    owner = os.environ.get("UM_UPDATE_STALLED_XRAY_DELETE_OWNER", "")
    epoch_raw = os.environ.get("UM_UPDATE_STALLED_XRAY_DELETE_EPOCH", "")
    try:
        epoch = int(epoch_raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("operation deployment hold is missing") from exc
    if not re.fullmatch(rf"op:{hold['operation_id']}:[1-9][0-9]*", owner) or epoch < 1:
        raise RuntimeError("operation deployment hold is invalid")
    return {**hold, "lease_owner": owner, "fencing_epoch": epoch}


def _validate_operation_hold(connection, hold: dict) -> None:
    from sqlalchemy import text

    operation_id = hold["operation_id"]
    _stalled_xray_delete(connection, operation_id)
    now = _database_now(connection.dialect.name)
    row = connection.execute(text(
        f"SELECT lease_owner,fencing_epoch,leased_until >= {now} AS live FROM resource_locks "
        "WHERE resource_key=:resource_key"
    ), {"resource_key": f"provisioning_op:{operation_id}"}).mappings().one_or_none()
    if row is None or (row["lease_owner"], int(row["fencing_epoch"]), bool(row["live"])) != (
            hold["lease_owner"], hold["fencing_epoch"], True):
        raise RuntimeError("stalled-operation deployment hold is not live")


def _acquire_operation_hold(engine, operation_id: int) -> dict:
    from sqlalchemy import text

    # Reserved owner marker: the matching superadmin recovery action may
    # atomically take over this exact one-hour hold. It is not a credential.
    owner = f"op:{operation_id}:{DEPLOYMENT_HOLD_NONCE}"
    now = _database_now(engine.dialect.name)
    if engine.dialect.name == "sqlite":
        deadline = f"strftime('%Y-%m-%d %H:%M:%f','now','+{OPERATION_HOLD_TTL_SECONDS} seconds')"
    else:
        deadline = f"TIMESTAMPADD(SECOND,{OPERATION_HOLD_TTL_SECONDS},NOW(6))"
    with engine.begin() as connection:
        _stalled_xray_delete(connection, operation_id)
        result = connection.execute(text(
            f"UPDATE resource_locks SET lease_owner=:owner,fencing_epoch=fencing_epoch+1,"
            f"leased_until={deadline},heartbeat_at={now},version=version+1 "
            "WHERE resource_key=:resource_key AND (lease_owner IS NULL OR leased_until < " + now + ")"
        ), {"owner": owner, "resource_key": f"provisioning_op:{operation_id}"})
        if result.rowcount != 1:
            raise RuntimeError("stalled provisioning operation is busy")
        epoch = connection.execute(text(
            "SELECT fencing_epoch FROM resource_locks WHERE resource_key=:resource_key "
            "AND lease_owner=:owner"
        ), {"resource_key": f"provisioning_op:{operation_id}", "owner": owner}).scalar_one()
        hold = {"operation_id": operation_id, "lease_owner": owner, "fencing_epoch": int(epoch)}
        _stalled_xray_delete(connection, operation_id, lock=True)
        _validate_operation_hold(connection, hold)
    return hold


def _release_operation_hold(engine, hold: dict) -> None:
    from sqlalchemy import text

    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE resource_locks SET lease_owner=NULL,leased_until=NULL,version=version+1 "
            "WHERE resource_key=:resource_key AND lease_owner=:owner AND fencing_epoch=:epoch"
        ), {"resource_key": f"provisioning_op:{hold['operation_id']}",
            "owner": hold["lease_owner"], "epoch": hold["fencing_epoch"]})
        if result.rowcount != 1:
            raise RuntimeError("stalled-operation deployment hold was lost")


def runtime_snapshot(engine, operation_hold: dict | None = None) -> dict:
    from sqlalchemy import bindparam, inspect, text
    inspector = inspect(engine)
    result = {}
    with engine.connect() as connection:
        for table, fields in RUNTIME_FIELDS.items():
            if not inspector.has_table(table):
                result[table] = None
                continue
            columns = {col["name"] for col in inspector.get_columns(table)}
            selected = [field for field in fields if field in columns]
            if not selected:
                result[table] = None
                continue
            row = connection.execute(text(f"SELECT {', '.join(selected)} FROM {table} WHERE id=1")).mappings().first()
            result[table] = dict(row) if row is not None else None
        if inspector.has_table("provisioning_type_modes"):
            result["provisioning_type_modes"] = [list(row) for row in connection.execute(text(
                "SELECT operation_type, mode FROM provisioning_type_modes ORDER BY operation_type"
            ))]
        # Readiness observations only, not an atomic traffic drain.
        for table, terminal in (
            ("provisioning_operations", NONTERMINAL_PROVISIONING_STATES),
            ("wallet_operations", NONTERMINAL_WALLET_STATES),
        ):
            if inspector.has_table(table):
                if table == "provisioning_operations" and operation_hold is not None:
                    _validate_operation_hold(connection, operation_hold)
                    continue
                query = text(f"SELECT COUNT(*) FROM {table} WHERE state NOT IN :terminal").bindparams(bindparam("terminal", expanding=True))
                if connection.execute(query, {"terminal": terminal}).scalar_one():
                    raise RuntimeError("unfinished durable operations")
    return result


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def configured_engine(database_url: str):
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    url = make_url(database_url)
    if url.get_backend_name() == "sqlite":
        if not url.database or url.database == ":memory:" or not Path(url.database).is_file():
            raise RuntimeError("configured panel database missing")
        uri = "file:" + quote(str(Path(url.database).resolve())) + "?mode=ro"
        return create_engine("sqlite://", creator=lambda: sqlite3.connect(uri, uri=True))
    return create_engine(database_url)


def create_backup(tag: str) -> dict:
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from app.config import settings
    if not re.fullmatch(r"update_[A-Za-z0-9_]+", tag):
        raise ValueError("invalid backup tag")
    os.umask(0o077)
    root = Path(os.environ.get("BACKUP_DIR", "/app/data/backups")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / tag
    folder.mkdir(mode=0o700)
    engine = configured_engine(settings.database_url)
    operation_hold = None
    backup_complete = False
    try:
        requested_hold = _parse_operation_hold(os.environ.get("UM_UPDATE_STALLED_XRAY_DELETE_ID", ""))
        if requested_hold is not None:
            operation_hold = _acquire_operation_hold(engine, requested_hold["operation_id"])
        state = runtime_snapshot(engine, operation_hold=operation_hold)
        url = make_url(settings.database_url)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            sqlite_backup(Path(url.database), folder / "panel.db")
            panel = "panel.db"
        elif url.get_backend_name() in ("mysql", "mariadb"):
            with engine.connect() as connection:
                count = connection.execute(text(
                    "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = DATABASE() "
                    "AND table_type = 'BASE TABLE' AND engine <> 'InnoDB'"
                )).scalar_one()
            if count:
                raise RuntimeError("consistent dump requires InnoDB")
            # Reuse dump primitive only: no backup rotation or Telegram delivery.
            from app.services.backup import _mysql_dump_bytes
            sql = _mysql_dump_bytes()
            if not sql.strip():
                raise RuntimeError("empty database dump")
            (folder / "panel.sql").write_bytes(sql)
            panel = "panel.sql"
        else:
            raise RuntimeError("unsupported database")
        sqlite_backup(Path(os.environ.get("BOT_DB_PATH", "/app/data/bot_data.db")), folder / "bot.db")
        (folder / "runtime.json").write_text(json.dumps(state, sort_keys=True))
        files = {name: {"sha256": digest(folder / name), "size": (folder / name).stat().st_size}
                 for name in (panel, "bot.db", "runtime.json")}
        manifest = {"directory": str(folder), "files": files,
                    "panel_validation": "sqlite_integrity" if panel.endswith(".db") else "dump_exit_status_and_sha256_not_restore_test"}
        if operation_hold is not None:
            manifest["operation_hold"] = operation_hold
        (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        backup_complete = True
        return manifest
    finally:
        if operation_hold is not None and not backup_complete:
            try:
                _release_operation_hold(engine, operation_hold)
            except Exception:
                # The bounded DB-clock lease expires automatically. Never
                # replace the original backup error with cleanup diagnostics.
                pass
        engine.dispose()


def release_operation_hold_from_environment() -> None:
    from app.config import settings

    hold = _hold_from_environment()
    if hold is None:
        raise RuntimeError("operation deployment hold is missing")
    engine = configured_engine(settings.database_url)
    try:
        _release_operation_hold(engine, hold)
    finally:
        engine.dispose()


def validate_operation_hold_from_environment() -> None:
    from app.config import settings

    hold = _hold_from_environment()
    if hold is None:
        raise RuntimeError("operation deployment hold is missing")
    engine = configured_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            _validate_operation_hold(connection, hold)
    finally:
        engine.dispose()


def verify_backup(folder: Path) -> None:
    manifest = json.loads((folder / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        if name not in ("panel.db", "panel.sql", "bot.db", "runtime.json"):
            raise RuntimeError("unexpected backup member")
        path = folder / name
        if path.stat().st_size != expected["size"] or digest(path) != expected["sha256"]:
            raise RuntimeError("backup copy mismatch")
        if name.endswith(".db"):
            db = sqlite3.connect("file:" + quote(str(path.resolve())) + "?mode=ro", uri=True)
            try:
                if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise RuntimeError("host backup integrity failed")
            finally:
                db.close()


def main() -> None:
    command = sys.argv[1]
    if command == "backup":
        print(json.dumps(create_backup(sys.argv[2]), sort_keys=True))
    elif command == "snapshot":
        from app.config import settings
        engine = configured_engine(settings.database_url)
        try:
            print(json.dumps(runtime_snapshot(engine, operation_hold=_hold_from_environment()), sort_keys=True))
        finally:
            engine.dispose()
    elif command == "release-operation-hold":
        release_operation_hold_from_environment()
        print("OPERATION_HOLD_RELEASED")
    elif command == "validate-operation-hold":
        validate_operation_hold_from_environment()
        print("OPERATION_HOLD_VALID")
    elif command == "verify-backup":
        verify_backup(Path(sys.argv[2]))
        print("BACKUP_VERIFIED")
    elif command == "compare":
        if json.loads(Path(sys.argv[2]).read_text()) != json.loads(Path(sys.argv[3]).read_text()):
            raise RuntimeError("runtime settings changed")
        print("RUNTIME_UNCHANGED")
    else:
        raise ValueError("unknown action")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # DB URLs/dump exceptions can contain credentials; never echo them.
        print("STOP: safe-update check failed (sensitive details suppressed)", file=sys.stderr)
        raise SystemExit(1)
