"""Admin CRUD for purchasable packages (quota/duration/price bundles) that
the sales bot shows to customers at checkout."""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin, require_permission

# Router-level dependency is just "logged in" - listing packages is
# available to every admin (needed to pick a package while creating a
# user, even for an admin without the "edit_packages" permission). Mutating
# endpoints split into "edit_packages" (create/update/files) and
# "delete_packages" (package delete only) - see permissions.py.
router = APIRouter(prefix="/api/packages", tags=["packages"], dependencies=[Depends(get_current_admin)])
_edit = Depends(require_permission("edit_packages"))
_delete = Depends(require_permission("delete_packages"))
_manage = _edit  # legacy alias

# Same persistent /app/data volume the sqlite DB itself lives on (see
# docker-compose.yml: ./backend/data:/app/data), so uploaded files survive
# container rebuilds/restarts just like everything else here.
PACKAGE_FILES_DIR = os.environ.get("PACKAGE_FILES_DIR", "/app/data/package_files")

# Telegram's Bot API itself caps documents sent by a bot at 50MB, so
# anything larger could never actually reach a customer anyway - capping
# uploads here also protects the /app/data volume (which the sqlite DB
# shares) from being filled by an oversized upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _sync_connections(db: Session, pkg: models.Package, specs: list[schemas.PackageConnectionSpec]) -> None:
    """Replaces the package's whole set of bundled server/service rows with
    the ones just submitted from the web UI (simplest correct semantics for
    an editable list - no per-row diffing needed)."""
    db.query(models.PackageConnection).filter(models.PackageConnection.package_id == pkg.id).delete()
    for spec in specs:
        db.add(models.PackageConnection(
            package_id=pkg.id,
            node_id=spec.node_id,
            protocol=spec.protocol,
            flow=spec.flow or "",
        ))


@router.get("", response_model=list[schemas.PackageOut])
def list_packages(db: Session = Depends(get_db)):
    return db.query(models.Package).order_by(models.Package.sort_order, models.Package.id).all()


@router.post("", response_model=schemas.PackageOut)
def create_package(payload: schemas.PackageCreate, db: Session = Depends(get_db), _perm=_edit):
    data = payload.model_dump(exclude={"connections"})
    pkg = models.Package(**data)
    db.add(pkg)
    db.flush()  # assign pkg.id before adding child rows
    _sync_connections(db, pkg, payload.connections)
    db.commit()
    db.refresh(pkg)
    return pkg


@router.put("/{package_id}", response_model=schemas.PackageOut)
def update_package(package_id: int, payload: schemas.PackageUpdate, db: Session = Depends(get_db), _perm=_edit):
    pkg = db.get(models.Package, package_id)
    if not pkg:
        raise HTTPException(404, "پکیج پیدا نشد")
    data = payload.model_dump(exclude_unset=True, exclude={"connections"})
    for k, v in data.items():
        setattr(pkg, k, v)
    if payload.connections is not None:
        _sync_connections(db, pkg, payload.connections)
    db.commit()
    db.refresh(pkg)
    return pkg


@router.delete("/{package_id}")
def delete_package(package_id: int, db: Session = Depends(get_db), _perm=_delete):
    pkg = db.get(models.Package, package_id)
    if not pkg:
        raise HTTPException(404, "پکیج پیدا نشد")
    for f in pkg.files:
        _unlink_quiet(f.stored_path)
    db.delete(pkg)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------- files
def _unlink_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass  # already gone / never existed - nothing to clean up


@router.post("/{package_id}/files", response_model=schemas.PackageFileOut)
def upload_package_file(package_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), _perm=_edit):
    """Attaches a file (VPN config, setup guide, installer, ...) to a
    package - the built-in sales bot sends every attached file to the
    customer automatically right after they buy/renew this package (see
    telegram_bot/handlers/customer.py and admin_pending.py)."""
    pkg = db.get(models.Package, package_id)
    if not pkg:
        raise HTTPException(404, "پکیج پیدا نشد")

    pkg_dir = os.path.join(PACKAGE_FILES_DIR, str(package_id))
    os.makedirs(pkg_dir, exist_ok=True)
    original_name = file.filename or "file"
    ext = os.path.splitext(original_name)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    stored_path = os.path.join(pkg_dir, stored_name)

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

    pf = models.PackageFile(
        package_id=package_id,
        filename=original_name,
        stored_path=stored_path,
        content_type=file.content_type,
        size_bytes=size,
    )
    db.add(pf)
    db.commit()
    db.refresh(pf)
    return pf


@router.delete("/{package_id}/files/{file_id}")
def delete_package_file(package_id: int, file_id: int, db: Session = Depends(get_db), _perm=_edit):
    pf = db.get(models.PackageFile, file_id)
    if not pf or pf.package_id != package_id:
        raise HTTPException(404, "فایل پیدا نشد")
    _unlink_quiet(pf.stored_path)
    db.delete(pf)
    db.commit()
    return {"ok": True}
