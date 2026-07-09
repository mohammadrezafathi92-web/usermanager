"""Manual database backup - admin-triggered from the panel's Settings
page. Creates a fresh backup (same safe sqlite-backup-API path the
automatic 4x/day job uses), best-effort sends it to the bot's Telegram
admins, and streams it back as a browser download in the same request."""
import os
import threading

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..deps import require_permission, require_superadmin
from ..services import backup as backup_service

router = APIRouter(prefix="/api/backup", tags=["backup"], dependencies=[Depends(require_permission("manage_settings"))])


@router.post("/run")
def run_backup():
    """Creates a new backup, best-effort sends it to Telegram admins, and
    returns it as a direct file download."""
    path = backup_service.create_backup()
    sent, total = backup_service.send_backup_to_telegram(path)
    response = FileResponse(
        path,
        media_type="application/gzip",
        filename=path.name,
        headers={"X-Telegram-Sent": str(sent), "X-Telegram-Total": str(total)},
    )
    return response


@router.get("/list")
def list_backups():
    return backup_service.list_backups()


@router.get("/download/{filename}")
def download_backup(filename: str):
    # No path separators allowed - filenames always come from list_backups()
    # (backup_YYYYmmdd_HHMMSS.db.gz), this just blocks path traversal.
    if "/" in filename or "\\" in filename or not filename.startswith("backup_"):
        raise HTTPException(400, "نام فایل نامعتبر است")
    path = backup_service.BACKUP_DIR / filename
    if not path.exists():
        raise HTTPException(404, "فایل بک‌آپ پیدا نشد")
    return FileResponse(path, media_type="application/gzip", filename=filename)


@router.post("/restore", dependencies=[Depends(require_superadmin)])
async def restore_backup(file: UploadFile = File(...)):
    """Superadmin-only: uploads a .db.gz (or raw .db) backup and fully
    replaces the live database with it. The current live db is
    safety-backed-up first. Forces the backend process to exit right after
    responding so Docker's `restart: unless-stopped` brings it back up
    against the newly-restored file - see restore_from_upload's docstring
    for why an in-process reload isn't enough."""
    data = await file.read()
    if not data:
        raise HTTPException(400, "فایل خالی است")
    try:
        backup_service.restore_from_upload(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    def _delayed_exit():
        import time

        time.sleep(1.5)
        os._exit(0)

    threading.Thread(target=_delayed_exit, daemon=True).start()
    return {"ok": True, "message": "دیتابیس با موفقیت جایگزین شد - سرویس در حال راه‌اندازی مجدد است..."}
