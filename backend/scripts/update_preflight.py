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


def runtime_snapshot(engine) -> dict:
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
            ("provisioning_operations", ("completed", "compensated")),
            ("wallet_operations", ("completed", "failed", "cancelled")),
        ):
            if inspector.has_table(table):
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
    try:
        state = runtime_snapshot(engine)
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
        (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
        return manifest
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
            print(json.dumps(runtime_snapshot(engine), sort_keys=True))
        finally:
            engine.dispose()
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
