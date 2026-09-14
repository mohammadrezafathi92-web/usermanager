"""Admin CRUD for the Mini App's shelves (models.PackageGroup).

Small on purpose. A group is a name, a line of text, an order and a switch;
everything interesting about it lives in the scoping, which is copied
wholesale from routers/packages.py rather than reinvented - the same
hierarchy.accessible_package_owner_ids for reads, the same
"owner_admin_id is derived from who is asking, never taken from the
payload" for writes, and the same 404 (not 403) for something outside the
caller's tree, so a reseller cannot even learn that another reseller's
group exists by probing ids.

Level-3 Sellers can read groups - they need the names to make sense of the
packages they sell - but cannot create or change them, exactly as with
packages themselves.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import get_current_admin
from ..services import hierarchy

router = APIRouter(
    prefix="/api/package-groups", tags=["package-groups"],
    dependencies=[Depends(get_current_admin)],
)


def _require_manager(admin: models.AdminUser) -> None:
    if hierarchy.is_seller(admin):
        raise HTTPException(403, "فروشنده‌ها اجازه ساخت یا ویرایش دسته‌بندی را ندارند")


def _get_or_404(db: Session, group_id: int, admin: models.AdminUser) -> models.PackageGroup:
    group = db.get(models.PackageGroup, group_id)
    if not group:
        raise HTTPException(404, "دسته‌بندی پیدا نشد")
    # Plain Python set membership, so `None in allowed` means exactly what
    # it says - unlike the SQL .in_() pitfall the list query below avoids.
    if group.owner_admin_id not in hierarchy.accessible_package_owner_ids(admin):
        raise HTTPException(404, "دسته‌بندی پیدا نشد")
    return group


@router.get("", response_model=list[schemas.PackageGroupOut])
def list_groups(db: Session = Depends(get_db), admin: models.AdminUser = Depends(get_current_admin)):
    allowed = hierarchy.accessible_package_owner_ids(admin)
    # NOT a plain .in_(allowed): that set legitimately contains None for
    # superadmin-owned rows, and SQL's `IN (NULL, ...)` never matches a
    # NULL column - the bug that once emptied every non-superadmin's
    # package list. See hierarchy.owner_id_in_clause.
    groups = (
        db.query(models.PackageGroup)
        .filter(hierarchy.owner_id_in_clause(models.PackageGroup.owner_admin_id, allowed))
        .order_by(models.PackageGroup.sort_order, models.PackageGroup.id)
        .all()
    )
    out = []
    for group in groups:
        item = schemas.PackageGroupOut.model_validate(group)
        # How many plans are on this shelf - so the panel can say
        # «۳ پکیج» on the row instead of making the admin go and count.
        item.package_count = (
            db.query(models.Package).filter(models.Package.group_id == group.id).count()
        )
        out.append(item)
    return out


@router.post("", response_model=schemas.PackageGroupOut)
def create_group(
    payload: schemas.PackageGroupCreate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    _require_manager(admin)
    data = payload.model_dump()
    # Derived from who is creating it, never read from the payload - the
    # same rule as routers/packages.py's create_package, and the reason a
    # reseller cannot plant a group in someone else's tree.
    data["owner_admin_id"] = None if admin.is_superadmin else hierarchy.parent_admin_scope_id(admin)
    group = models.PackageGroup(**data)
    db.add(group)
    db.commit()
    db.refresh(group)
    return schemas.PackageGroupOut.model_validate(group)


@router.put("/{group_id}", response_model=schemas.PackageGroupOut)
def update_group(
    group_id: int,
    payload: schemas.PackageGroupUpdate,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    _require_manager(admin)
    group = _get_or_404(db, group_id, admin)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    db.commit()
    db.refresh(group)
    return schemas.PackageGroupOut.model_validate(group)


@router.delete("/{group_id}")
def delete_group(
    group_id: int,
    db: Session = Depends(get_db),
    admin: models.AdminUser = Depends(get_current_admin),
):
    """Removes the shelf, never what was on it.

    Package.group_id is ON DELETE SET NULL, and the loop below does the same
    thing explicitly rather than trusting the FK - SQLite only enforces
    foreign keys when PRAGMA foreign_keys is on, which is not something to
    bet a reseller's whole catalogue on. The packages become ungrouped,
    which the Mini App shows under «سایر پلن‌ها», so they stay on sale.
    """
    _require_manager(admin)
    group = _get_or_404(db, group_id, admin)
    orphaned = (
        db.query(models.Package)
        .filter(models.Package.group_id == group.id)
        .update({models.Package.group_id: None}, synchronize_session=False)
    )
    db.delete(group)
    db.commit()
    return {"ok": True, "ungrouped_packages": orphaned}
