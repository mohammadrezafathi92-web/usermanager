"""Superadmin-only CRUD for other admin accounts - creating restricted
sub-admins, editing their checkbox permissions/login link, resetting their
password, and removing them. Every endpoint here is gated behind
require_superadmin (see deps.py) - a sub-admin can never reach this router
at all, regardless of what `permissions` they're granted, so they can never
escalate their own or anyone else's access through the regular API."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_superadmin
from ..security import hash_password
from ..permissions import PERMISSION_CHOICES, parse_permissions, format_permissions, effective_permissions

router = APIRouter(prefix="/api/admins", tags=["admins"], dependencies=[Depends(require_superadmin)])


def _validate_permissions(perms: list[str]) -> str:
    unknown = [p for p in perms if p not in PERMISSION_CHOICES]
    if unknown:
        raise HTTPException(400, f"دسترسی نامعتبر: {', '.join(unknown)}")
    return format_permissions(set(perms))


def _out(db: Session, admin: models.AdminUser) -> schemas.AdminOut:
    users_count = db.query(models.User).filter(models.User.owner_admin_id == admin.id).count()
    return schemas.AdminOut(
        id=admin.id,
        username=admin.username,
        is_superadmin=admin.is_superadmin,
        # Effective permissions (from the group if assigned, else the
        # admin's own checkboxes) - what actually governs their access.
        permissions=sorted(effective_permissions(admin)),
        login_slug=admin.login_slug,
        balance=admin.balance or 0,
        telegram_id=admin.telegram_id,
        created_at=admin.created_at,
        users_count=users_count,
        group_id=admin.group_id,
        group_name=admin.group.name if admin.group else None,
    )


def _group_out(db: Session, group: models.AdminPermissionGroup) -> schemas.AdminGroupOut:
    admins_count = db.query(models.AdminUser).filter(models.AdminUser.group_id == group.id).count()
    return schemas.AdminGroupOut(
        id=group.id,
        name=group.name,
        permissions=sorted(parse_permissions(group.permissions)),
        admins_count=admins_count,
    )


# ---------- Permission groups ----------
# Registered before the "" (list admins) route below on purpose, but since
# the path prefix differs ("/groups" vs plain "") there's no collision -
# kept together here so groups management sits right next to admin CRUD.
@router.get("/groups", response_model=list[schemas.AdminGroupOut])
def list_groups(db: Session = Depends(get_db)):
    groups = db.query(models.AdminPermissionGroup).order_by(models.AdminPermissionGroup.id).all()
    return [_group_out(db, g) for g in groups]


@router.post("/groups", response_model=schemas.AdminGroupOut)
def create_group(payload: schemas.AdminGroupCreate, db: Session = Depends(get_db)):
    name = payload.name.strip()
    if not name:
        raise HTTPException(400, "نام گروه نمی‌تواند خالی باشد")
    if db.query(models.AdminPermissionGroup).filter(models.AdminPermissionGroup.name == name).first():
        raise HTTPException(400, "گروهی با این نام قبلا ساخته شده است")
    group = models.AdminPermissionGroup(name=name, permissions=_validate_permissions(payload.permissions))
    db.add(group)
    db.commit()
    db.refresh(group)
    return _group_out(db, group)


@router.put("/groups/{group_id}", response_model=schemas.AdminGroupOut)
def update_group(group_id: int, payload: schemas.AdminGroupUpdate, db: Session = Depends(get_db)):
    group = db.get(models.AdminPermissionGroup, group_id)
    if not group:
        raise HTTPException(404, "گروه پیدا نشد")
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(400, "نام گروه نمی‌تواند خالی باشد")
        clash = db.query(models.AdminPermissionGroup).filter(
            models.AdminPermissionGroup.name == name, models.AdminPermissionGroup.id != group.id
        ).first()
        if clash:
            raise HTTPException(400, "گروهی با این نام قبلا ساخته شده است")
        group.name = name
    if payload.permissions is not None:
        group.permissions = _validate_permissions(payload.permissions)
    db.commit()
    db.refresh(group)
    return _group_out(db, group)


@router.delete("/groups/{group_id}")
def delete_group(group_id: int, db: Session = Depends(get_db)):
    group = db.get(models.AdminPermissionGroup, group_id)
    if not group:
        raise HTTPException(404, "گروه پیدا نشد")
    # Admins in this group aren't deleted - just detached, so they fall
    # back to their own individual `permissions` checkboxes (which are
    # preserved even while a group is assigned, so nothing is lost here).
    db.query(models.AdminUser).filter(models.AdminUser.group_id == group.id).update(
        {"group_id": None}, synchronize_session=False
    )
    db.delete(group)
    db.commit()
    return {"ok": True}


@router.get("", response_model=list[schemas.AdminOut])
def list_admins(db: Session = Depends(get_db)):
    admins = db.query(models.AdminUser).order_by(models.AdminUser.id).all()
    return [_out(db, a) for a in admins]


@router.get("/permission-choices")
def permission_choices():
    """Feeds the frontend's checkbox list - keeps the human-readable labels
    defined in one place (permissions.py) instead of duplicated in JS."""
    return PERMISSION_CHOICES


@router.post("", response_model=schemas.AdminOut)
def create_admin(payload: schemas.AdminCreate, db: Session = Depends(get_db)):
    if db.query(models.AdminUser).filter(models.AdminUser.username == payload.username).first():
        raise HTTPException(400, "این نام کاربری قبلا ثبت شده است")
    if len(payload.password) < 6:
        raise HTTPException(400, "رمز عبور باید حداقل ۶ کاراکتر باشد")
    slug = (payload.login_slug or "").strip() or None
    if slug and db.query(models.AdminUser).filter(models.AdminUser.login_slug == slug).first():
        raise HTTPException(400, "این لینک ورود قبلا برای ادمین دیگری استفاده شده است")
    if payload.telegram_id is not None and db.query(models.AdminUser).filter(
        models.AdminUser.telegram_id == payload.telegram_id
    ).first():
        raise HTTPException(400, "این آیدی تلگرام قبلا برای ادمین دیگری ثبت شده است")
    group_id = payload.group_id or None
    if group_id and not db.get(models.AdminPermissionGroup, group_id):
        raise HTTPException(400, "گروه انتخاب‌شده پیدا نشد")

    admin = models.AdminUser(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        is_superadmin=False,
        permissions=_validate_permissions(payload.permissions),
        login_slug=slug,
        telegram_id=payload.telegram_id,
        group_id=group_id,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.put("/{admin_id}", response_model=schemas.AdminOut)
def update_admin(admin_id: int, payload: schemas.AdminUpdate, db: Session = Depends(get_db)):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "دسترسی ادمین اصلی از این بخش قابل تغییر نیست")

    if payload.password is not None:
        if len(payload.password) < 6:
            raise HTTPException(400, "رمز عبور باید حداقل ۶ کاراکتر باشد")
        admin.hashed_password = hash_password(payload.password)
    if payload.permissions is not None:
        admin.permissions = _validate_permissions(payload.permissions)
    if payload.login_slug is not None:
        slug = payload.login_slug.strip() or None
        if slug:
            clash = db.query(models.AdminUser).filter(
                models.AdminUser.login_slug == slug, models.AdminUser.id != admin.id
            ).first()
            if clash:
                raise HTTPException(400, "این لینک ورود قبلا برای ادمین دیگری استفاده شده است")
        admin.login_slug = slug
    if payload.telegram_id is not None:
        # 0 (or any falsy-but-not-None value the form might send) is treated
        # as "clear it" - the same convention User.telegram_id editing uses
        # elsewhere, since 0 is never a real Telegram user id.
        tg_id = payload.telegram_id or None
        if tg_id:
            clash = db.query(models.AdminUser).filter(
                models.AdminUser.telegram_id == tg_id, models.AdminUser.id != admin.id
            ).first()
            if clash:
                raise HTTPException(400, f"این آیدی تلگرام قبلا برای ادمین «{clash.username}» ثبت شده است")
        admin.telegram_id = tg_id
    if payload.balance is not None:
        admin.balance = payload.balance
    if payload.group_id is not None:
        group_id = payload.group_id or None
        if group_id and not db.get(models.AdminPermissionGroup, group_id):
            raise HTTPException(400, "گروه انتخاب‌شده پیدا نشد")
        admin.group_id = group_id

    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.delete("/{admin_id}")
def delete_admin(
    admin_id: int,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_superadmin),
):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.id == current.id:
        raise HTTPException(400, "نمی‌توانید حساب خودتان را حذف کنید")
    if admin.is_superadmin:
        raise HTTPException(400, "ادمین اصلی قابل حذف نیست")

    # Users this admin owned aren't deleted - just unassigned (visible to
    # superadmins only, like any never-assigned user) so nobody's VPN
    # service is silently destroyed just because the admin managing them
    # was removed. A superadmin can reassign them via the user edit form.
    db.query(models.User).filter(models.User.owner_admin_id == admin.id).update(
        {"owner_admin_id": None}, synchronize_session=False
    )
    db.delete(admin)
    db.commit()
    return {"ok": True}
