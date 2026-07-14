"""Admin CRUD for "آموزش" (tutorial) entries - a title + free text + any
number of attached photos/videos, shown to customers from the sales bot's
"📚 آموزش" menu (see telegram_bot/handlers/tutorials.py)."""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session, joinedload

from .. import models, schemas
from ..database import get_db
from ..deps import require_permission

# Router-level gate is "view_tutorials" (every endpoint at minimum needs
# that, matching the panel's own /tutorials page being reachable at all) -
# mutating endpoints additionally require "edit_tutorials" or
# "delete_tutorials" per action, see permissions.py for why this used to be
# one broad "manage_tutorials" toggle.
router = APIRouter(prefix="/api/tutorials", tags=["tutorials"], dependencies=[Depends(require_permission("view_tutorials"))])
_edit = Depends(require_permission("edit_tutorials"))
_delete = Depends(require_permission("delete_tutorials"))

# Same persistent /app/data volume the sqlite DB and package files already
# live on (see docker-compose.yml: ./backend/data:/app/data).
TUTORIAL_MEDIA_DIR = os.environ.get("TUTORIAL_MEDIA_DIR", "/app/data/tutorial_media")

# Telegram's Bot API caps documents/videos sent by a bot at 50MB, so
# anything larger could never actually reach a customer anyway - capping
# uploads here also protects the /app/data volume (which the sqlite DB
# shares) from being filled by an oversized upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".3gp"}


def _guess_kind(filename: str, content_type: str | None) -> str:
    if content_type and content_type.startswith("video/"):
        return "video"
    ext = os.path.splitext(filename or "")[1].lower()
    return "video" if ext in _VIDEO_EXTS else "photo"


def _unlink_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@router.get("", response_model=list[schemas.TutorialOut])
def list_tutorials(db: Session = Depends(get_db)):
    return (
        db.query(models.Tutorial)
        .options(joinedload(models.Tutorial.media), joinedload(models.Tutorial.software))
        .order_by(models.Tutorial.sort_order, models.Tutorial.id)
        .all()
    )


@router.post("", response_model=schemas.TutorialOut)
def create_tutorial(payload: schemas.TutorialCreate, db: Session = Depends(get_db), _perm=_edit):
    t = models.Tutorial(**payload.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


@router.put("/{tutorial_id}", response_model=schemas.TutorialOut)
def update_tutorial(tutorial_id: int, payload: schemas.TutorialUpdate, db: Session = Depends(get_db), _perm=_edit):
    t = db.get(models.Tutorial, tutorial_id)
    if not t:
        raise HTTPException(404, "آموزش پیدا نشد")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(t, k, v)
    db.commit()
    db.refresh(t)
    return t


@router.delete("/{tutorial_id}")
def delete_tutorial(tutorial_id: int, db: Session = Depends(get_db), _perm=_delete):
    t = db.get(models.Tutorial, tutorial_id)
    if not t:
        raise HTTPException(404, "آموزش پیدا نشد")
    for m in t.media:
        _unlink_quiet(m.stored_path)
    for s in t.software:
        if s.stored_path:
            _unlink_quiet(s.stored_path)
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.post("/{tutorial_id}/media", response_model=schemas.TutorialMediaOut)
def upload_tutorial_media(
    tutorial_id: int,
    file: UploadFile = File(...),
    kind: str = Form(""),
    db: Session = Depends(get_db),
    _perm=_edit,
):
    t = db.get(models.Tutorial, tutorial_id)
    if not t:
        raise HTTPException(404, "آموزش پیدا نشد")

    resolved_kind = kind if kind in ("photo", "video") else _guess_kind(file.filename or "", file.content_type)

    t_dir = os.path.join(TUTORIAL_MEDIA_DIR, str(tutorial_id))
    os.makedirs(t_dir, exist_ok=True)
    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = os.path.join(t_dir, stored_name)

    size = 0
    too_large = False
    with open(stored_path, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        _unlink_quiet(stored_path)
        raise HTTPException(400, f"حجم فایل نباید بیشتر از {MAX_UPLOAD_BYTES // (1024 * 1024)} مگابایت باشد")

    m = models.TutorialMedia(
        tutorial_id=tutorial_id,
        kind=resolved_kind,
        filename=original_name,
        stored_path=stored_path,
        content_type=file.content_type,
        size_bytes=size,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


@router.delete("/{tutorial_id}/media/{media_id}")
def delete_tutorial_media(tutorial_id: int, media_id: int, db: Session = Depends(get_db), _perm=_delete):
    m = db.get(models.TutorialMedia, media_id)
    if not m or m.tutorial_id != tutorial_id:
        raise HTTPException(404, "فایل پیدا نشد")
    _unlink_quiet(m.stored_path)
    db.delete(m)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# دانلود نرم‌افزار (see models.TutorialSoftware's docstring) - a link-only
# entry (POST "") and/or an uploaded file (POST ".../file"), both attached
# to the same tutorial and both shown together in the bot.
TUTORIAL_SOFTWARE_DIR = os.environ.get("TUTORIAL_SOFTWARE_DIR", "/app/data/tutorial_software")


@router.post("/{tutorial_id}/software", response_model=schemas.TutorialSoftwareOut)
def create_tutorial_software_link(tutorial_id: int, payload: schemas.TutorialSoftwareCreate, db: Session = Depends(get_db), _perm=_edit):
    t = db.get(models.Tutorial, tutorial_id)
    if not t:
        raise HTTPException(404, "آموزش پیدا نشد")
    if not (payload.url or "").strip():
        raise HTTPException(400, "لینک نمی‌تواند خالی باشد")
    s = models.TutorialSoftware(
        tutorial_id=tutorial_id, name=payload.name, url=payload.url.strip(), sort_order=payload.sort_order,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.post("/{tutorial_id}/software/file", response_model=schemas.TutorialSoftwareOut)
def upload_tutorial_software_file(
    tutorial_id: int,
    file: UploadFile = File(...),
    name: str = Form(...),
    db: Session = Depends(get_db),
    _perm=_edit,
):
    t = db.get(models.Tutorial, tutorial_id)
    if not t:
        raise HTTPException(404, "آموزش پیدا نشد")

    t_dir = os.path.join(TUTORIAL_SOFTWARE_DIR, str(tutorial_id))
    os.makedirs(t_dir, exist_ok=True)
    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = os.path.join(t_dir, stored_name)

    size = 0
    too_large = False
    with open(stored_path, "wb") as out:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                too_large = True
                break
            out.write(chunk)
    if too_large:
        _unlink_quiet(stored_path)
        raise HTTPException(400, f"حجم فایل نباید بیشتر از {MAX_UPLOAD_BYTES // (1024 * 1024)} مگابایت باشد")

    s = models.TutorialSoftware(
        tutorial_id=tutorial_id,
        name=name,
        filename=original_name,
        stored_path=stored_path,
        content_type=file.content_type,
        size_bytes=size,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


@router.delete("/{tutorial_id}/software/{software_id}")
def delete_tutorial_software(tutorial_id: int, software_id: int, db: Session = Depends(get_db), _perm=_delete):
    s = db.get(models.TutorialSoftware, software_id)
    if not s or s.tutorial_id != tutorial_id:
        raise HTTPException(404, "مورد پیدا نشد")
    if s.stored_path:
        _unlink_quiet(s.stored_path)
    db.delete(s)
    db.commit()
    return {"ok": True}
