"""Admin CRUD for purchasable packages (quota/duration/price bundles) that
the sales bot shows to customers at checkout."""
import os
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin, require_permission
from ..services import hierarchy

# Router-level dependency is just "logged in" - listing packages is
# available to every admin (needed to pick a package while creating a
# user, even for an admin without the "edit_packages" permission), further
# narrowed to hierarchy-visible packages below (see
# hierarchy.accessible_package_owner_ids's docstring: everyone, INCLUDING
# a superadmin now, only ever sees their own scope - NULL/global packages
# for a superadmin, their own tree for an Admin/Seller, never each other's).
# Mutating endpoints split into "edit_packages" (create/update/files) and
# "delete_packages" (package delete only) - see permissions.py - AND
# restricted to superadmin or level-2 Admin only: level-3 Sellers can see/
# use packages to provision users but never create/edit/delete them
# themselves (they only have whatever their parent Admin built and shared).
router = APIRouter(prefix="/api/packages", tags=["packages"], dependencies=[Depends(get_current_admin)])
_edit = Depends(require_permission("edit_packages"))
_delete = Depends(require_permission("delete_packages"))
_manage = _edit  # legacy alias


def _require_package_manager(admin: models.AdminUser) -> None:
    if hierarchy.is_seller(admin):
        raise HTTPException(403, "فروشنده‌ها اجازه ساخت یا ویرایش پکیج را ندارند")


def _out(pkg: models.Package, my_price: int | None = None) -> models.Package:
    """Bolts the owner's username (and, for a Seller caller, their own
    resale price override - see models.PackageSellerPrice) onto the ORM
    object as plain attributes right before returning it, since neither is
    a real column on Package - PackageOut.model_validate reads them
    straight off via from_attributes, same trick as routers/admins.py's
    _out()."""
    pkg.owner_admin_username = pkg.owner_admin.username if pkg.owner_admin else None
    pkg.my_price = my_price
    return pkg


def _get_scoped_package(db: Session, package_id: int, admin: models.AdminUser) -> models.Package:
    """Fetches a package AND checks it's in this admin's hierarchy scope -
    404s (not 403) for an out-of-scope package, same pattern as
    routers/nodes.py's _get_scoped_node."""
    pkg = db.get(models.Package, package_id)
    if not pkg:
        raise HTTPException(404, "پکیج پیدا نشد")
    allowed = hierarchy.accessible_package_owner_ids(admin)
    # `None in allowed` legitimately means "owner_admin_id IS NULL is
    # allowed" - a plain `pkg.owner_admin_id not in allowed` check works
    # fine here (unlike the SQL .in_() pitfall below) since this is a
    # regular Python set membership test, not a query filter. Also 404s a
    # superadmin trying to reach an Admin's/Seller's own package by id
    # directly (e.g. PUT/DELETE) now that accessible_package_owner_ids no
    # longer returns "unrestricted" for a superadmin either - see its
    # docstring for why.
    if pkg.owner_admin_id not in allowed:
        raise HTTPException(404, "پکیج پیدا نشد")
    return pkg

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
def list_packages(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    allowed = hierarchy.accessible_package_owner_ids(admin)
    # NOT a plain `.in_(allowed)` - that set legitimately contains None
    # (global/superadmin-owned packages), and SQL's `IN (NULL, ...)` never
    # matches a NULL column, so every non-superadmin Admin/Seller was
    # silently seeing an empty package list whenever they only had access
    # to global packages (see hierarchy.owner_id_in_clause). This filter now
    # applies to a superadmin too (allowed = {None}) - see
    # accessible_package_owner_ids's docstring for why: a superadmin only
    # ever sees their own "global" packages here, never an Admin's/
    # Seller's, matching every other per-tenant resource.
    q = db.query(models.Package).filter(hierarchy.owner_id_in_clause(models.Package.owner_admin_id, allowed))
    pkgs = q.order_by(models.Package.sort_order, models.Package.id).all()
    # A Seller sees their own resale price override (if set) next to each
    # package's base price - fetched in one query rather than N+1.
    my_prices: dict[int, int] = {}
    if hierarchy.is_seller(admin):
        my_prices = {
            row.package_id: row.price
            for row in db.query(models.PackageSellerPrice).filter(models.PackageSellerPrice.seller_admin_id == admin.id).all()
        }
    return [_out(p, my_prices.get(p.id)) for p in pkgs]


@router.post("", response_model=schemas.PackageOut)
def create_package(payload: schemas.PackageCreate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    _require_package_manager(admin)
    data = payload.model_dump(exclude={"connections"})
    # owner_admin_id is always derived from who's creating it, never taken
    # from the payload - a superadmin's packages stay global (NULL), a
    # level-2 Admin's packages are scoped to themselves (see
    # hierarchy.parent_admin_scope_id - a Seller can never reach here at
    # all thanks to _require_package_manager above).
    data["owner_admin_id"] = None if admin.is_superadmin else hierarchy.parent_admin_scope_id(admin)
    pkg = models.Package(**data)
    db.add(pkg)
    db.flush()  # assign pkg.id before adding child rows
    _sync_connections(db, pkg, payload.connections)
    db.commit()
    db.refresh(pkg)
    return _out(pkg)


@router.put("/{package_id}", response_model=schemas.PackageOut)
def update_package(package_id: int, payload: schemas.PackageUpdate, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    _require_package_manager(admin)
    pkg = _get_scoped_package(db, package_id, admin)
    data = payload.model_dump(exclude_unset=True, exclude={"connections"})
    data.pop("owner_admin_id", None)  # ownership never changes via this endpoint
    for k, v in data.items():
        setattr(pkg, k, v)
    if payload.connections is not None:
        _sync_connections(db, pkg, payload.connections)
    db.commit()
    db.refresh(pkg)
    return _out(pkg)


@router.delete("/{package_id}")
def delete_package(package_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_delete):
    _require_package_manager(admin)
    pkg = _get_scoped_package(db, package_id, admin)
    for f in pkg.files:
        _unlink_quiet(f.stored_path)
    db.delete(pkg)
    db.commit()
    return {"ok": True}


@router.put("/{package_id}/my-price", response_model=schemas.PackageOut)
def set_my_package_price(
    package_id: int,
    payload: schemas.SellerPackagePriceUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    """A level-3 Seller's OWN resale price override for a package they can
    already see/use (their parent Admin's, or a global one) - shown/charged
    instead of the package's base `price` in the Seller's own Telegram bot
    (see routers/bot.py's list_packages). Deliberately NOT gated behind
    edit_packages/_require_package_manager - a Seller is never editing the
    package itself (still entirely the parent Admin's), only recording
    their own resale number, which is exactly what Sellers are supposed to
    be able to do (unlike node/package management, which stays fully
    Admin-only)."""
    if not hierarchy.is_seller(admin):
        raise HTTPException(403, "این قابلیت فقط برای فروشنده‌هاست")
    pkg = _get_scoped_package(db, package_id, admin)  # 404s if out of this Seller's scope

    row = (
        db.query(models.PackageSellerPrice)
        .filter(models.PackageSellerPrice.package_id == package_id, models.PackageSellerPrice.seller_admin_id == admin.id)
        .first()
    )
    if payload.price is None:
        if row:
            db.delete(row)
            db.commit()
        return _out(pkg, None)

    if payload.price < 0:
        raise HTTPException(400, "قیمت نمی‌تواند منفی باشد")
    if row:
        row.price = payload.price
    else:
        row = models.PackageSellerPrice(package_id=package_id, seller_admin_id=admin.id, price=payload.price)
        db.add(row)
    db.commit()
    return _out(pkg, payload.price)


# ------------------------------------------------------------------- files
def _unlink_quiet(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass  # already gone / never existed - nothing to clean up


@router.post("/{package_id}/files", response_model=schemas.PackageFileOut)
def upload_package_file(package_id: int, file: UploadFile = File(...), db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    """Attaches a file (VPN config, setup guide, installer, ...) to a
    package - the built-in sales bot sends every attached file to the
    customer automatically right after they buy/renew this package (see
    telegram_bot/handlers/customer.py and admin_pending.py)."""
    _require_package_manager(admin)
    pkg = _get_scoped_package(db, package_id, admin)

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
def delete_package_file(package_id: int, file_id: int, db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin), _perm=_edit):
    _require_package_manager(admin)
    _get_scoped_package(db, package_id, admin)  # scope check, 404s if out of reach
    pf = db.get(models.PackageFile, file_id)
    if not pf or pf.package_id != package_id:
        raise HTTPException(404, "فایل پیدا نشد")
    _unlink_quiet(pf.stored_path)
    db.delete(pf)
    db.commit()
    return {"ok": True}
