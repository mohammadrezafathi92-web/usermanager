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
from ..deps import require_admin_or_above, require_superadmin, require_confirm_password
from ..security import hash_password
from ..services import hierarchy, accounting
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
    # Accounting mirror of the same event (see services/accounting.py) -
    # added to the same uncommitted transaction, so it can never drift from
    # the AdminBalanceLog row above.
    accounting.record(
        db, "admin_credit_change", amount,
        admin_id=admin.id, actor_admin_id=actor_id, note=note,
    )
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
def delete_group(group_id: int, db: Session = Depends(get_db), _s=Depends(require_superadmin), _confirm=Depends(require_confirm_password)):
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
    """Superadmin-only: sets WHERE an account sits and WHAT it is, as two
    separate things.

    They used to be one thing. Role was derived from parent_admin_id, so
    "give this account a parent" and "demote this account" were the same
    operation and could not be told apart. On this panel's live data that
    produced eight accounts that are main resellers in practice, created
    under the superadmin's own row, and therefore permanently classified as
    level-3 Sellers - holding node grants that accessible_node_ids throws
    away, because a Seller's node set is empty by definition. Nothing was
    misconfigured. The model had no way to say what they were.

    So `role` is now its own field. Omitting it means the role stays
    exactly as it is, and only the position changes. hierarchy.
    validate_placement holds the one rule that still couples them - the
    three-level limit on how far resources can nest.

    Demoting an Admin who has their own Sellers still promotes those
    Sellers first, the same "unassign, don't destroy" handling delete_admin
    uses, so nobody is left pointing at a parent that just became a Seller.
    That cascade now keys off the new ROLE rather than off gaining a
    parent - otherwise making an account an Admin under the superadmin
    would scatter its own Sellers for no reason."""
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.is_superadmin:
        raise HTTPException(400, "نقش ادمین اصلی قابل تغییر نیست")

    new_parent_id = payload.parent_admin_id
    parent = None
    if new_parent_id is not None:
        if new_parent_id == admin.id:
            raise HTTPException(400, "یک ادمین نمی‌تواند والد خودش باشد")
        parent = db.get(models.AdminUser, new_parent_id)
        if parent is None:
            raise HTTPException(400, "ادمین والد پیدا نشد")
        if parent.id in hierarchy.subtree_ids(db, admin):
            # Moving an account under its own descendant would make a cycle,
            # which rebuild_path survives but every prefix query would then
            # give nonsense answers about who can see whom.
            raise HTTPException(400, "این حساب را نمی‌شود زیر زیرمجموعه‌ی خودش برد")

    # Role is now taken from the request, not deduced from the parent.
    # Omitted means keep whatever it is: moving an account should change
    # where it sits, never what it may do. That coupling is what forced
    # eight of this panel's resellers into the Seller role.
    new_role = (payload.role or "").strip() or hierarchy.role(admin)
    problem = hierarchy.validate_placement(new_role, parent)
    if problem:
        raise HTTPException(400, problem)

    # Becoming a Seller is what forces the cascade, not merely gaining a
    # parent - an Admin sitting under the superadmin keeps its own Sellers
    # and its own packages, which is the whole point of this phase.
    demoting_to_seller = new_role == hierarchy.ROLE_SELLER
    if demoting_to_seller:
        db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id == admin.id).update(
            {"parent_admin_id": None}, synchronize_session=False
        )
        # This account is being demoted into a level-3 Seller, who can never
        # own Packages/Tutorials themselves (see routers/packages.py's
        # _require_package_manager) - without reassigning its own
        # Package/Tutorial rows, they'd keep owner_admin_id == admin.id,
        # but accessible_package_owner_ids/accessible_tutorial_owner_ids for
        # a Seller now resolves to their PARENT's id instead - so this
        # admin would instantly lose visibility into packages/tutorials
        # they themselves built, and nobody else (not even the new parent)
        # could see them either. Reassigning to the new parent keeps them
        # usable by exactly the people who should now have them: this
        # demoted Seller (via their parent's scope) and their new parent
        # Admin's whole tree.
        #
        # A superadmin parent is the exception: every package the superadmin
        # owns is stored with owner_admin_id NULL, never their real id (see
        # create_package and accessible_package_owner_ids). Handing these
        # rows the superadmin's numeric id instead would put them in a scope
        # nobody resolves to, and they would vanish from every list.
        inherit_owner_id = None if (parent is not None and parent.is_superadmin) else new_parent_id
        db.query(models.Package).filter(models.Package.owner_admin_id == admin.id).update(
            {"owner_admin_id": inherit_owner_id}, synchronize_session=False
        )
        db.query(models.Tutorial).filter(models.Tutorial.owner_admin_id == admin.id).update(
            {"owner_admin_id": inherit_owner_id}, synchronize_session=False
        )

    admin.parent_admin_id = new_parent_id
    admin.role = new_role
    hierarchy.rebuild_path(db, admin)

    # Sellers promoted to roots above were bulk-updated, so their stored
    # role and path are both stale. They become Admins because that is the
    # only legal role for a parentless account (validate_placement), and
    # their paths must be rebuilt from their new absent parent.
    if demoting_to_seller:
        for child in db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id.is_(None)).all():
            if child.is_superadmin:
                continue
            if hierarchy.role(child) == hierarchy.ROLE_SELLER:
                child.role = hierarchy.ROLE_ADMIN
            hierarchy.rebuild_path(db, child)
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
            # A Seller's parent may be an existing level-2 Admin, OR the
            # superadmin themself (parent.is_superadmin) - a low-trust
            # reseller the superadmin wants to supervise directly, gated
            # by the normal granular `permissions` checkboxes instead of a
            # level-2 Admin's full-tree bypass. Never another Seller
            # (would create a 4th level - fixed at exactly 3, see
            # services/hierarchy.py).
            valid_parent = parent and (parent.is_superadmin or hierarchy.role(parent) == hierarchy.ROLE_ADMIN)
            if not valid_parent:
                raise HTTPException(400, "ادمین والد باید ادمین اصلی یا یک ادمین سطح ۲ معتبر باشد")
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
    # The role is whatever the caller asked for, and only falls back to the
    # old position-based guess when they did not say.
    #
    # That fallback is why this endpoint needs the same treatment as
    # reparent: without it, "create an Admin directly under the superadmin"
    # is unexpressible at creation time too, and every such account would
    # have to be created wrong and then promoted - which is exactly the
    # history behind the eight accounts this phase is fixing.
    #
    # A non-superadmin caller can only ever create their own Sellers
    # (parent_admin_id was forced to current.id above), so the role is
    # forced too rather than trusted - a level-2 Admin must not be able to
    # mint a peer.
    requested = (getattr(payload, "role", None) or "").strip()
    if not current.is_superadmin:
        admin.role = hierarchy.ROLE_SELLER
    else:
        admin.role = requested or hierarchy.derive_role(admin)
        problem = hierarchy.validate_placement(
            admin.role,
            db.get(models.AdminUser, parent_admin_id) if parent_admin_id else None,
        )
        if problem:
            raise HTTPException(400, problem)
    hierarchy.rebuild_path(db, admin, cascade=False)

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
    current: models.AdminUser = Depends(require_admin_or_above), _confirm=Depends(require_confirm_password)):
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    if admin.id == current.id:
        raise HTTPException(400, "نمی‌توانید حساب خودتان را حذف کنید")
    if admin.is_superadmin:
        raise HTTPException(400, "ادمین اصلی قابل حذف نیست")
    _scope_or_403(current, admin)

    # Everything this account held is INHERITED BY ITS PARENT rather than
    # cut loose.
    #
    # It used to be cut loose: children had parent_admin_id set to NULL and
    # customers had owner_admin_id set to NULL. For a Seller that was
    # plainly wrong - their parent Admin was right there, still running,
    # and still the person responsible for those customers - and it is the
    # most likely source of the 577 ownerless customers on this install.
    # An ownerless customer is visible to the superadmin alone, so from the
    # reseller's side their customers simply vanished.
    #
    # Deleting a ROOT account is the one case with genuinely nowhere to put
    # things; NULL there keeps its old meaning of "the superadmin's pool".
    heir_id = admin.parent_admin_id
    heir = db.get(models.AdminUser, heir_id) if heir_id else None
    # A superadmin parent owns packages/tutorials as NULL, never by id (see
    # create_package) - the same translation reparent_admin needs.
    resource_heir_id = None if (heir is not None and heir.is_superadmin) else heir_id

    children = (
        db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id == admin.id).all()
    )
    for child in children:
        child.parent_admin_id = heir_id
        # A parentless account cannot legally be a Seller - it would have
        # no scope to inherit and would see nothing at all (see
        # hierarchy.validate_placement). Only then is it promoted.
        if heir_id is None and hierarchy.role(child) == hierarchy.ROLE_SELLER:
            child.role = hierarchy.ROLE_ADMIN
        hierarchy.rebuild_path(db, child)

    # Customers are never deleted, only handed over - nobody's VPN service
    # should stop working because the person who sold it was removed.
    db.query(models.User).filter(models.User.owner_admin_id == admin.id).update(
        {"owner_admin_id": heir_id}, synchronize_session=False
    )
    # Packages/Tutorials this admin owned need the SAME "don't destroy"
    # treatment as Users above - without this, they silently become
    # invisible to EVERYONE (not even the superadmin, unlike an orphaned
    # User) once admin.id no longer belongs to any account:
    # hierarchy.accessible_package_owner_ids/accessible_tutorial_owner_ids
    # only ever match an EXACT owner_admin_id (a real admin's id, or None
    # for the superadmin's own global scope) - a dangling id left behind
    # by this delete matches nobody's scope at all. Unlike Users, Package/
    # Tutorial has no separate "orphaned, superadmin-visible" state distinct
    # from "owned by the superadmin" - NULL already means exactly that (see
    # routers/packages.py's create_package: `None if admin.is_superadmin
    # else ...`), so falling back to NULL here reuses that same convention
    # instead of inventing a new one: the superadmin regains visibility and
    # can keep or reassign them by hand, same as they can for orphaned
    # Users.
    db.query(models.Package).filter(models.Package.owner_admin_id == admin.id).update(
        {"owner_admin_id": resource_heir_id}, synchronize_session=False
    )
    db.query(models.Tutorial).filter(models.Tutorial.owner_admin_id == admin.id).update(
        {"owner_admin_id": resource_heir_id}, synchronize_session=False
    )
    db.delete(admin)
    db.commit()
    return {"ok": True}


@router.get("/{admin_id}/delete-impact")
def delete_impact(
    admin_id: int,
    db: Session = Depends(get_db),
    current: models.AdminUser = Depends(require_admin_or_above),
):
    """What deleting this account would move, and to whom.

    Exists because the counts are the whole decision and they were
    invisible: the confirm dialog asked for a password without ever saying
    that 240 customers were about to change hands, or that three Sellers
    were about to be promoted. Read-only, and it runs the same inheritance
    rule delete_admin does rather than describing it in words that could
    drift from the code.
    """
    admin = db.get(models.AdminUser, admin_id)
    if not admin:
        raise HTTPException(404, "ادمین پیدا نشد")
    _scope_or_403(current, admin)

    heir = db.get(models.AdminUser, admin.parent_admin_id) if admin.parent_admin_id else None
    children = db.query(models.AdminUser).filter(models.AdminUser.parent_admin_id == admin.id).all()
    return {
        "username": admin.username,
        "role": hierarchy.role(admin),
        "heir_id": heir.id if heir else None,
        "heir_username": heir.username if heir else None,
        "customers": db.query(models.User).filter(models.User.owner_admin_id == admin.id).count(),
        "packages": db.query(models.Package).filter(models.Package.owner_admin_id == admin.id).count(),
        "children": [
            {"id": c.id, "username": c.username,
             # A child only changes role when there is no heir to inherit it.
             "promoted": heir is None and hierarchy.role(c) == hierarchy.ROLE_SELLER}
            for c in children
        ],
        "balance": admin.balance or 0,
    }
