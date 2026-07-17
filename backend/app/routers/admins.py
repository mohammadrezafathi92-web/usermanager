"""CRUD for other admin accounts, hierarchy-aware (see services/
hierarchy.py): a superadmin creates/manages level-2 Admins, and a level-2
Admin creates/manages their OWN level-3 Sellers through this SAME router -
gated behind require_admin_or_above (see deps.py) instead of the old
superadmin-only require_superadmin. Every mutating endpoint additionally
checks `_scope_or_403` so a level-2 Admin can only ever touch their own
Sellers, never another Admin's, and can never create/edit anyone but a
Seller (their own tier). A Seller can never reach this router at all -
require_admin_or_above rejects them outright, so they can never escalate
their own or anyone else's access through the regular API."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..deps import require_admin_or_above, require_superadmin
from ..security import hash_password
from ..services import hierarchy
from ..permissions import PERMISSION_CHOICES, PERMISSION_GROUPS, parse_permissions, format_permissions, effective_permissions

router = APIRouter(prefix="/api/admins", tags=["admins"], dependencies=[Depends(require_admin_or_above)])


def _validate_permissions(perms: list[str]) -> str:
    unknown = [p for p in perms if p not in PERMISSION_CHOICES]
    if unknown:
        raise HTTPException(400, f"دسترسی نامعتبر: {', '.join(unknown)}")
    return format_permissions(set(perms))


def _scope_or_403(current: models.AdminUser, target: models.AdminUser) -> None:
    """A superadmin may touch any level-2 Admin or level-3 Seller. A
    level-2 Admin may only touch their OWN Sellers (target.parent_admin_id
    == current.id) - never another Admin, another Admin's Sellers, or a
    superadmin. Sellers never reach this router at all (require_admin_or_above)."""
    if current.is_superadmin:
        return
    if target.parent_admin_id == current.id and hierarchy.is_seller(target):
        return
    raise HTTPException(403, "شما دسترسی به این حساب را ندارید")


def _out(db: Session, admin: models.AdminUser) -> schemas.AdminOut:
    users_count = db.query(models.User).filter(models.User.owner_admin_id == admin.id).count()
    node_ids = [
        row.node_id
        for row in db.query(models.AdminNodeAccess.node_id).filter(models.AdminNodeAccess.admin_id == admin.id).all()
    ]
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
        billing_mode=admin.billing_mode or "flat",
        volume_balance_gb=admin.volume_balance_gb,
        role=hierarchy.role(admin),
        parent_admin_id=admin.parent_admin_id,
        parent_admin_username=admin.parent_admin.username if admin.parent_admin else None,
        accessible_node_ids=node_ids,
    )


def _group_out(db: Session, group: models.AdminPermissionGroup) -> schemas.AdminGroupOut:
    admins_count = db.query(models.AdminUser).filter(models.AdminUser.group_id == group.id).count()
    return schemas.AdminGroupOut(
        id=group.id,
        name=group.name,
        permissions=sorted(parse_permissions(group.permissions)),
        admins_count=admins_count,
    )


def _log_out(log: models.AdminBalanceLog) -> schemas.AdminBalanceLogOut:
    return schemas.AdminBalanceLogOut(
        id=log.id,
        admin_id=log.admin_id,
        amount=log.amount,
        balance_after=log.balance_after,
        note=log.note,
        created_by_username=log.created_by.username if log.created_by else None,
        created_at=log.created_at,
    )


def _volume_log_out(log: models.AdminVolumeLog) -> schemas.AdminVolumeLogOut:
    return schemas.AdminVolumeLogOut(
        id=log.id,
        admin_id=log.admin_id,
        amount_gb=log.amount_gb,
        balance_after_gb=log.balance_after_gb,
        note=log.note,
        created_by_username=log.created_by.username if log.created_by else None,
        created_at=log.created_at,
    )


def _apply_volume_change(db: Session, admin: models.AdminUser, amount_gb: float, note: str | None, actor_id: int | None) -> models.AdminVolumeLog:
    """Volume-pool equivalent of _apply_balance_change below - used by both
    the initial "حجم پایه" (at creation, when billing_mode="usage") and the
    manual افزایش/کاهش حجم endpoint."""
    admin.volume_balance_gb = (admin.volume_balance_gb or 0) + amount_gb
    log = models.AdminVolumeLog(
        admin_id=admin.id,
        amount_gb=amount_gb,
        balance_after_gb=admin.volume_balance_gb,
        note=note,
        created_by_id=actor_id,
    )
    db.add(log)
    return log


def _apply_balance_change(db: Session, admin: models.AdminUser, amount: int, note: str | None, actor_id: int | None) -> models.AdminBalanceLog:
    """Shared by the initial "اعتبار پایه" (at creation) and the manual
    "افزایش/کاهش اعتبار" endpoint below - always moves the balance AND
    writes the matching audit row in the same transaction, so the two can
    never drift apart."""
    admin.balance = (admin.balance or 0) + amount
    log = models.AdminBalanceLog(
        admin_id=admin.id,
        amount=amount,
        balance_after=admin.balance,
        note=note,
        created_by_id=actor_id,
    )
    db.add(log)
    return log


# ---------- Permission groups ----------
# Superadmin-only, deliberately not hierarchy-scoped like the rest of this
# router (groups have no owner_admin_id - kept as one global, shared list
# for simplicity). A level-2 Admin can still grant their own Sellers
# individual per-page permissions directly (the `permissions` list on
# AdminCreate/AdminUpdate) without needing a group. Registered before the
# "" (list admins) route below on purpose, but since the path prefix
# differs ("/groups" vs plain "") there's no collision - kept together here
# so groups management sits right next to admin CRUD.
@router.get("/groups", response_model=list[schemas.AdminGroupOut])
def list_groups(db: Session = Depends(get_db), _s=Depends(require_superadmin)):
    groups = db.query(models.AdminPermissionGroup).order_by(models.AdminPermissionGroup.id).all()
    return [_group_out(db, g) for g in groups]


@router.post("/groups", response_model=schemas.AdminGroupOut)
def create_group(payload: schemas.AdminGroupCreate, db: Session = Depends(get_db), _s=Depends(require_superadmin)):
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
def update_group(group_id: int, payload: schemas.AdminGroupUpdate, db: Session = Depends(get_db), _s=Depends(require_superadmin)):
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
def delete_group(group_id: int, db: Session = Depends(get_db), _s=Depends(require_superadmin)):
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
def list_admins(db: Session = Depends(get_db), current: models.AdminUser = Depends(require_admin_or_above)):
    """Superadmin sees every level-2 Admin AND every level-3 Seller (full
    oversight). A level-2 Admin sees ONLY their own Sellers - never other
    Admins, other Admins' Sellers, or the superadmin itself."""
    if current.is_superadmin:
        admins = db.query(models.AdminUser).filter(models.AdminUser.id != current.id).order_by(models.AdminUser.id).all()
    else:
        admins = (
            db.query(models.AdminUser)
            .filter(models.AdminUser.parent_admin_id == current.id)
            .order_by(models.AdminUser.id)
            .all()
        )
    return [_out(db, a) for a in admins]


@router.get("/available-nodes", response_model=list[schemas.NodeOut])
def list_available_nodes_for_assignment(db: Session = Depends(get_db), _s=Depends(require_superadmin)):
    """Every node, for the superadmin's node-assignment UI (see
    set_admin_nodes below) - a level-2 Admin's OWN node list (Nodes.jsx) is
    already scoped separately in routers/nodes.py."""
    return db.query(models.Node).order_by(models.Node.id).all()


@router.put("/{admin_id}/nodes", response_model=schemas.AdminOut)
def set_admin_nodes(
    admin_id: int,
    payload: schemas.AdminNodeAccessUpdate,
    db: Session = Depends(get_db),
    _s: models.AdminUser = Depends(require_superadmin),
):
    """Full-replace which nodes a level-2 Admin can see/use (see
    models.AdminNodeAccess) - superadmin only, since nodes are always
    created/configured by a superadmin (services/hierarchy.py's
    accessible_node_ids docstring)."""
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin or hierarchy.role(admin) != hierarchy.ROLE_ADMIN:
        raise HTTPException(400, "اختصاص سرور فقط برای ادمین‌های سطح ۲ معنا دارد")
    valid_ids = {
        row.id for row in db.query(models.Node.id).filter(models.Node.id.in_(payload.node_ids)).all()
    }
    unknown = set(payload.node_ids) - valid_ids
    if unknown:
        raise HTTPException(400, f"سرور نامعتبر: {sorted(unknown)}")
    db.query(models.AdminNodeAccess).filter(models.AdminNodeAccess.admin_id == admin_id).delete(synchronize_session=False)
    for node_id in valid_ids:
        db.add(models.AdminNodeAccess(admin_id=admin_id, node_id=node_id))
    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.put("/{admin_id}/reparent", response_model=schemas.AdminOut)
def reparent_admin(
    admin_id: int,
    payload: schemas.AdminReparentRequest,
    db: Session = Depends(get_db),
    _s: models.AdminUser = Depends(require_superadmin),
):
    """Superadmin-only: reclassifies an EXISTING account between tiers -
    set parent_admin_id=None to make/keep it a level-2 Admin, or to an
    existing level-2 Admin's id to make/move it into that Admin's level-3
    Seller. This is exactly the "این ادمین‌های قبلی که ساختم در واقع باید
    فروشنده بشن" gap: before this endpoint, every non-superadmin account
    was permanently stuck as whatever tier auto-migration/creation gave
    it, with no way to reclassify it afterward.

    Fixed-3-levels rule (see services/hierarchy.py) still applies: the
    target itself must not be a superadmin, and the new parent (if any)
    must be an existing level-2 Admin - never a Seller (would create a
    4th level) and never itself.

    Demoting a level-2 Admin who already has their OWN Sellers into
    someone else's Seller would leave those Sellers pointing at a
    "grandparent" that no longer makes sense - so exactly like
    delete_admin's existing "unassign, don't destroy" handling, their
    Sellers are first promoted to level-2 Admins in their own right
    (parent_admin_id=None) before the demotion itself is applied."""
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "نقش ادمین اصلی قابل تغییر نیست")

    new_parent_id = payload.parent_admin_id
    if new_parent_id is not None:
        if new_parent_id == admin.id:
            raise HTTPException(400, "یک ادمین نمی‌تواند والد خودش باشد")
        parent = db.get(models.AdminUser, new_parent_id)
        if not parent or parent.is_superadmin or hierarchy.role(parent) != hierarchy.ROLE_ADMIN:
            raise HTTPException(400, "ادمین والد باید یک ادمین سطح ۲ معتبر باشد")

    # This account currently has its own Sellers (i.e. it's a level-2
    # Admin being demoted) - promote them first so nobody ends up 4 levels
    # deep or pointing at a parent that just became a Seller itself.
    if new_parent_id is not None:
        db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id == admin.id).update(
            {"parent_admin_id": None}, synchronize_session=False
        )

    admin.parent_admin_id = new_parent_id
    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.get("/permission-choices")
def permission_choices():
    """Feeds the frontend's checkbox list - keeps the human-readable labels
    defined in one place (permissions.py) instead of duplicated in JS.
    Grouped by page (PERMISSION_GROUPS) since task #230 expanded this from
    4 flat toggles to granular per-page + per-action permissions - the
    frontend renders one section per group. A flat "choices" map (old
    shape) is also included for backward compatibility with any code still
    expecting it."""
    return {"groups": PERMISSION_GROUPS, "choices": PERMISSION_CHOICES}


@router.post("", response_model=schemas.AdminOut)
def create_admin(
    payload: schemas.AdminCreate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_admin_or_above),
):
    """Who gets created is mostly derived from WHO's calling, never freely
    from the payload (mirrors is_superadmin never being client-settable):
    a level-2 Admin ALWAYS creates their own level-3 Seller
    (parent_admin_id=current.id) - payload.parent_admin_id is ignored for
    them entirely. A superadmin creates a level-2 Admin by default
    (parent_admin_id=None), OR may directly create a level-3 Seller under
    an existing level-2 Admin by setting payload.parent_admin_id (see
    schemas.AdminCreate) - handy for "این ادمینی که تازه ساختم در واقع
    باید فروشنده‌ی فلان ادمین باشه" without a separate reparent step.
    Sellers can never reach this endpoint at all (require_admin_or_above)."""
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
    billing_mode = payload.billing_mode if payload.billing_mode in ("flat", "usage") else "flat"
    if current.is_superadmin:
        parent_admin_id = None
        if payload.parent_admin_id is not None:
            parent = db.get(models.AdminUser, payload.parent_admin_id)
            if not parent or parent.is_superadmin or hierarchy.role(parent) != hierarchy.ROLE_ADMIN:
                raise HTTPException(400, "ادمین والد باید یک ادمین سطح ۲ معتبر باشد")
            parent_admin_id = parent.id
    else:
        parent_admin_id = current.id

    admin = models.AdminUser(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        is_superadmin=False,
        parent_admin_id=parent_admin_id,
        permissions=_validate_permissions(payload.permissions),
        login_slug=slug,
        telegram_id=payload.telegram_id,
        group_id=group_id,
        billing_mode=billing_mode,
    )
    db.add(admin)
    db.flush()  # assigns admin.id, needed for the balance/volume log FKs below

    if payload.initial_balance:
        _apply_balance_change(db, admin, payload.initial_balance, "اعتبار پایه اولیه", actor_id=current.id)
    if billing_mode == "usage" and payload.initial_volume_gb:
        _apply_volume_change(db, admin, payload.initial_volume_gb, "حجم پایه اولیه", actor_id=current.id)

    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.put("/{admin_id}", response_model=schemas.AdminOut)
def update_admin(
    admin_id: int,
    payload: schemas.AdminUpdate,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_admin_or_above),
):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "دسترسی ادمین اصلی از این بخش قابل تغییر نیست")
    _scope_or_403(current, admin)

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
        # Deprecated absolute-set path (predates the logged topup endpoint
        # below) - kept working for API compatibility, but still recorded
        # as a balance-log entry (delta = new - old) so no balance change
        # can happen silently/unlogged regardless of which endpoint made it.
        delta = payload.balance - (admin.balance or 0)
        if delta:
            _apply_balance_change(db, admin, delta, "ویرایش مستقیم موجودی", actor_id=current.id)
    if payload.group_id is not None:
        group_id = payload.group_id or None
        if group_id and not db.get(models.AdminPermissionGroup, group_id):
            raise HTTPException(400, "گروه انتخاب‌شده پیدا نشد")
        admin.group_id = group_id
    if payload.billing_mode is not None:
        if payload.billing_mode not in ("flat", "usage"):
            raise HTTPException(400, "مدل قیمت‌گذاری نامعتبر است")
        admin.billing_mode = payload.billing_mode

    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.post("/{admin_id}/topup", response_model=schemas.AdminOut)
def topup_admin_balance(admin_id: int, payload: schemas.AdminTopupRequest, db: Session = Depends(get_db), current: models.AdminUser = Depends(require_admin_or_above)):
    """The proper, always-logged way to change a reseller's wholesale
    credit balance - positive amount = افزایش اعتبار, negative = manual
    correction/deduction. Every call here creates exactly one
    AdminBalanceLog row (see _apply_balance_change)."""
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "اعتبار برای ادمین اصلی معنا ندارد")
    _scope_or_403(current, admin)
    if not payload.amount:
        raise HTTPException(400, "مبلغ نمی‌تواند صفر باشد")
    _apply_balance_change(db, admin, payload.amount, (payload.note or "").strip() or None, actor_id=current.id)
    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.get("/{admin_id}/balance-logs", response_model=list[schemas.AdminBalanceLogOut])
def list_admin_balance_logs(admin_id: int, db: Session = Depends(get_db), current: models.AdminUser = Depends(require_admin_or_above)):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    _scope_or_403(current, admin)
    logs = (
        db.query(models.AdminBalanceLog)
        .filter(models.AdminBalanceLog.admin_id == admin_id)
        .order_by(models.AdminBalanceLog.id.desc())
        .all()
    )
    return [_log_out(l) for l in logs]


@router.post("/{admin_id}/volume-topup", response_model=schemas.AdminOut)
def topup_admin_volume(admin_id: int, payload: schemas.AdminVolumeTopupRequest, db: Session = Depends(get_db), current: models.AdminUser = Depends(require_admin_or_above)):
    """Volume-pool equivalent of /topup above - only meaningful for
    billing_mode="usage" admins, but not hard-blocked for "flat" admins
    (a superadmin may top up the volume pool in advance of switching an
    admin to usage mode)."""
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "حجم برای ادمین اصلی معنا ندارد")
    _scope_or_403(current, admin)
    if not payload.amount_gb:
        raise HTTPException(400, "مقدار حجم نمی‌تواند صفر باشد")
    _apply_volume_change(db, admin, payload.amount_gb, (payload.note or "").strip() or None, actor_id=current.id)
    db.commit()
    db.refresh(admin)
    return _out(db, admin)


@router.get("/{admin_id}/volume-logs", response_model=list[schemas.AdminVolumeLogOut])
def list_admin_volume_logs(admin_id: int, db: Session = Depends(get_db), current: models.AdminUser = Depends(require_admin_or_above)):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    _scope_or_403(current, admin)
    logs = (
        db.query(models.AdminVolumeLog)
        .filter(models.AdminVolumeLog.admin_id == admin_id)
        .order_by(models.AdminVolumeLog.id.desc())
        .all()
    )
    return [_volume_log_out(l) for l in logs]


@router.get("/login-logs", response_model=list[schemas.AdminLoginLogOut])
def list_login_logs(
    admin_id: int | None = None,
    only_failed: bool = False,
    limit: int = 200,
    db: Session = Depends(get_db),
    _s: models.AdminUser = Depends(require_superadmin),
):
    """Superadmin-only IP-based login report (مورد ۵) - every login
    attempt against the panel, success or fail, including the superadmin's
    own logins. Deliberately NOT opened up to level-2 Admins even for their
    own Sellers - this is a security/audit surface, kept superadmin-only
    same as before the hierarchy feature. `admin_id` filters to one admin;
    `only_failed` narrows to rejected attempts (wrong password/unknown
    username) for spotting brute-force noise."""
    q = db.query(models.AdminLoginLog)
    if admin_id:
        q = q.filter(models.AdminLoginLog.admin_id == admin_id)
    if only_failed:
        q = q.filter(models.AdminLoginLog.success == False)  # noqa: E712
    logs = q.order_by(models.AdminLoginLog.id.desc()).limit(min(limit, 1000)).all()
    return [
        schemas.AdminLoginLogOut(
            id=l.id,
            admin_id=l.admin_id,
            admin_username=l.admin.username if l.admin else None,
            attempted_username=l.attempted_username,
            ip_address=l.ip_address,
            user_agent=l.user_agent,
            success=l.success,
            created_at=l.created_at,
        )
        for l in logs
    ]


@router.delete("/{admin_id}")
def delete_admin(
    admin_id: int,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_admin_or_above),
):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.id == current.id:
        raise HTTPException(400, "نمی‌توانید حساب خودتان را حذف کنید")
    if admin.is_superadmin:
        raise HTTPException(400, "ادمین اصلی قابل حذف نیست")
    _scope_or_403(current, admin)

    # If this is a level-2 Admin (superadmin deleting one), their own
    # Sellers aren't deleted either - clearing parent_admin_id promotes
    # them to level-2 Admins in their own right (see services/hierarchy.py's
    # role()) rather than leaving them orphaned with no scope at all. Not
    # ideal (they gain full access they didn't have before), but keeps
    # their accounts/users working instead of being silently cut off; a
    # superadmin can review/reassign them by hand afterward. Same
    # "unassign, don't destroy" philosophy as the users.owner_admin_id
    # handling below.
    db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id == admin.id).update(
        {"parent_admin_id": None}, synchronize_session=False
    )
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
