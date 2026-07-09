"""Full-database backup: safe consistent copies of the live SQLite file
(taken via sqlite3's online backup API, which is WAL-safe - a plain file
copy could grab a half-written page while the RADIUS server/poller/web API
are writing), gzip-compressed and written to /app/data/backups. Used by
both the automatic 4x/day job (main.py) and the manual "دریافت بک‌آپ فوری"
button in the panel (routers/backup.py)."""
from __future__ import annotations

import datetime as dt
import gzip
import logging
import os
import shutil
import sqlite3
from pathlib import Path

from ..config import settings
from ..database import SessionLocal
from .. import models

logger = logging.getLogger("backup")

BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/app/data/backups"))
KEEP_LAST = 40  # ~10 days of history at 4/day, plus manual ones in between


def _db_path() -> str:
    """Extracts the raw filesystem path out of a `sqlite:///...` /
    `sqlite:////...` URL. Falls back to the raw value for any other DB
    engine (not expected here, but keeps this from crashing outright)."""
    url = settings.database_url
    prefix = "sqlite:///"
    if url.startswith(prefix):
        # "sqlite:///relative.db" -> "relative.db" (3 slashes = relative)
        # "sqlite:////abs/path.db" -> "/abs/path.db" (4 slashes = absolute,
        # the leading "/" of the absolute path is the 4th slash left over)
        return url[len(prefix):]
    return url


def create_backup() -> Path:
    """Creates a fresh, consistent, gzip-compressed backup file and returns
    its path. Also prunes old backups beyond KEEP_LAST."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src_path = _db_path()

    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    tmp_db = BACKUP_DIR / f".tmp_{stamp}.db"
    final_path = BACKUP_DIR / f"backup_{stamp}.db.gz"

    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(str(tmp_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    with open(tmp_db, "rb") as f_in, gzip.open(final_path, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp_db.unlink(missing_ok=True)

    _cleanup_old_backups()
    return final_path


def _cleanup_old_backups() -> None:
    files = sorted(BACKUP_DIR.glob("backup_*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in files[KEEP_LAST:]:
        old.unlink(missing_ok=True)


def list_backups() -> list[dict]:
    if not BACKUP_DIR.exists():
        return []
    files = sorted(BACKUP_DIR.glob("backup_*.db.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [
        {
            "filename": p.name,
            "size_bytes": p.stat().st_size,
            "created_at": dt.datetime.utcfromtimestamp(p.stat().st_mtime).isoformat(),
        }
        for p in files
    ]


def _admin_telegram_ids() -> list[int]:
    db = SessionLocal()
    try:
        row = db.get(models.BotSettings, 1)
        if not row or not row.admin_ids:
            return []
        out = []
        for part in row.admin_ids.split(","):
            part = part.strip()
            if part:
                try:
                    out.append(int(part))
                except ValueError:
                    continue
        return out
    finally:
        db.close()


def send_backup_to_telegram(path: Path) -> tuple[int, int]:
    """Sends the given backup file to every configured bot admin. Returns
    (sent, total) - best-effort, never raises (e.g. bot not running/
    configured, or an admin blocked the bot)."""
    from ..telegram_bot import runner as telegram_bot_runner  # local import: avoids import cycle at module load

    admin_ids = _admin_telegram_ids()
    if not admin_ids:
        return 0, 0
    caption = f"💾 بک‌آپ دیتابیس — {dt.datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
    sent = 0
    for chat_id in admin_ids:
        ok = telegram_bot_runner.send_document_sync(chat_id, str(path), caption=caption)
        if ok:
            sent += 1
    return sent, len(admin_ids)


def restore_from_upload(data: bytes) -> None:
    """Validates an uploaded backup (accepts either the gzip'd .db.gz this
    app produces, or a raw .db file) and atomically replaces the live
    database with it. Raises ValueError (Persian message, safe to show the
    admin as-is) on any validation failure - nothing is touched in that
    case. On success the CURRENT live db is safety-backed-up first (via
    create_backup) so a bad restore can itself be undone.

    Note: this only swaps the file on disk. SQLAlchemy connections already
    open in this process keep reading the OLD file (POSIX rename doesn't
    affect already-open fds), so the caller MUST force a process restart
    right after calling this for the new data to actually take effect."""
    if data[:2] == b"\x1f\x8b":
        try:
            data = gzip.decompress(data)
        except OSError as exc:
            raise ValueError("فایل gzip قابل باز شدن نیست (فایل خراب یا ناقص است)") from exc

    if data[:16] != b"SQLite format 3\x00":
        raise ValueError("فایل انتخاب‌شده یک فایل بکاپ دیتابیس معتبر نیست")

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = BACKUP_DIR / ".tmp_restore_upload.db"
    tmp_path.write_bytes(data)

    try:
        conn = sqlite3.connect(str(tmp_path))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise ValueError(f"بررسی صحت فایل بکاپ ناموفق بود: {integrity}")
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            required = {"users", "admin_users", "nodes", "connections"}
            missing = required - tables
            if missing:
                raise ValueError("این فایل مربوط به دیتابیس این پنل نیست (جداول اصلی پیدا نشد)")
        finally:
            conn.close()

        # Safety net: keep a backup of the CURRENT live db before overwriting it.
        create_backup()

        src_path = Path(_db_path())
        os.replace(tmp_path, src_path)

        # Any -wal/-shm sidecars on disk belong to the OLD db we just
        # replaced - stale, and SQLite would try to replay them against the
        # new file on next open otherwise. Safe to just drop them; the
        # restored file is a full checkpointed snapshot on its own.
        Path(f"{src_path}-wal").unlink(missing_ok=True)
        Path(f"{src_path}-shm").unlink(missing_ok=True)
    finally:
        tmp_path.unlink(missing_ok=True)


def run_scheduled_backup() -> None:
    """Entry point for the APScheduler cron job (4x/day)."""
    try:
        path = create_backup()
        sent, total = send_backup_to_telegram(path)
        logger.info("backup created: %s (sent to %s/%s admins)", path.name, sent, total)
    except Exception:
        logger.exception("scheduled backup failed")
