"""Central rules for the 3-tier reseller hierarchy: superadmin -> admin
(level 2, full panel access scoped to their own tree) -> seller (level 3,
granular-permission scoped, works within their parent admin's packages/
nodes). Fixed at exactly 3 levels - see AdminUser.parent_admin_id's
docstring. Every router that needs to scope a query by "which admin(s) can
see this" should go through the helpers here instead of re-deriving the
same role/ownership logic locally, the same way permissions.py centralizes
effective_permissions()."""
from __future__ import annotations

from sqlalchemy import or_
from sqlalchemy.orm import Session

from .. import models

ROLE_SUPERADMIN = "superadmin"
ROLE_ADMIN = "admin"
ROLE_SELLER = "seller"


def role(admin: models.AdminUser) -> str:
    if admin.is_superadmin:
        return ROLE_SUPERADMIN
    if admin.parent_admin_id is None:
        # A non-superadmin with no parent is a level-2 Admin, created
        # directly by a superadmin.
        return ROLE_ADMIN
    return ROLE_SELLER


def is_seller(admin: models.AdminUser) -> bool:
    return role(admin) == ROLE_SELLER


def parent_admin_scope_id(admin: models.AdminUser) -> int | None:
    """The level-2 Admin whose tree this account belongs to - itself for a
    superadmin/admin, its parent for a seller. Used to resolve "which
    Admin's granted nodes / owned packages should this account use"."""
    if role(admin) == ROLE_SELLER:
        return admin.parent_admin_id
    return admin.id


def can_create_sub_admin(admin: models.AdminUser) -> bool:
    """Superadmins create level-2 Admins; level-2 Admins create their own
    level-3 Sellers. Sellers can never create anyone - the hierarchy is
    fixed at exactly 3 levels."""
    return role(admin) in (ROLE_SUPERADMIN, ROLE_ADMIN)


def owned_admin_ids(db: Session, admin: models.AdminUser) -> set[int] | None:
    """Which AdminUser ids' end-CUSTOMER users (models.User, never to be
    confused with AdminUser accounts themselves) this account may see/
    manage. Always returns a concrete set now, never None/unrestricted -
    NOT EVEN for a superadmin: each Admin's customer base is fully private
    to that Admin's own tree, on purpose (the whole point of "هر ادمین
    یوزرمنیجر شخصی خودش رو داشته باشه" - superadmin provisions/oversees the
    ADMINS and NODES, not their end customers). A superadmin only ever sees
    users they personally created themselves (owner_admin_id == their own
    id - rare, but possible via the "ساخت کاربر" form same as anyone else).
    An Admin (level 2) sees their own users AND every one of their
    Sellers' users (roll-up oversight of their own tree only).
    A Seller (level 3) only ever sees their own.

    NOTE: this is deliberately DIFFERENT from accessible_node_ids/
    accessible_package_owner_ids below, which stay unrestricted (None) for
    a superadmin - infrastructure (nodes) and catalog (packages) are still
    superadmin-administered; only the actual customer roster is walled
    off."""
    r = role(admin)
    if r == ROLE_SUPERADMIN or r == ROLE_SELLER:
        return {admin.id}
    seller_ids = [
        row.id
        for row in db.query(models.AdminUser.id).filter(models.AdminUser.parent_admin_id == admin.id).all()
    ]
    return {admin.id, *seller_ids}


def user_visibility_clause(db: Session, admin: models.AdminUser):
    """SQLAlchemy filter expression for models.User rows this account may
    see - use this (not a raw `.in_(owned_admin_ids(...))`) for any actual
    User query, since it also covers ORPHANED users (owner_admin_id IS
    NULL - e.g. left behind by delete_admin's "unassign, don't destroy"
    handling): those are only ever visible to a superadmin, who's the only
    one who CAN reassign them (see routers/admins.py). A plain `.in_(...)`
    can never match SQL NULL even if None were stuffed into the set, so
    this needs the explicit IS NULL clause below rather than just folding
    it into owned_admin_ids's returned set."""
    owned = owned_admin_ids(db, admin)
    clause = models.User.owner_admin_id.in_(owned)
    if admin.is_superadmin:
        clause = or_(clause, models.User.owner_admin_id.is_(None))
    return clause


def can_see_user(admin: models.AdminUser, owned: set[int], user_owner_admin_id: int | None) -> bool:
    """Single-object counterpart to user_visibility_clause, for callers
    that already have the User loaded (e.g. routers/users.py's
    _get_owned_user) and just need a plain bool instead of a query filter."""
    if user_owner_admin_id in owned:
        return True
    return admin.is_superadmin and user_owner_admin_id is None


def accessible_node_ids(db: Session, admin: models.AdminUser) -> set[int] | None:
    """Which Node ids this account may see/use.
    None = unrestricted (superadmin - sees/manages every node; nodes are
    always created/configured by a superadmin, see routers/nodes.py).
    An Admin (level 2) sees only nodes explicitly granted via
    AdminNodeAccess. A Seller (level 3) has NO direct node access at all -
    they only ever work through their parent Admin's already-built
    Packages, never pick a node by hand."""
    r = role(admin)
    if r == ROLE_SUPERADMIN:
        return None
    if r == ROLE_SELLER:
        return set()
    return {
        row.node_id
        for row in db.query(models.AdminNodeAccess.node_id).filter(models.AdminNodeAccess.admin_id == admin.id).all()
    }


def accessible_package_owner_ids(admin: models.AdminUser) -> set[int | None] | None:
    """Which Package.owner_admin_id values this account may see/use.
    None = unrestricted (superadmin sees every package regardless of
    owner). Everyone else sees global packages (owner_admin_id IS NULL,
    made by a superadmin) plus their own tree's Admin-owned packages: an
    Admin sees their own; a Seller sees their parent Admin's (Sellers never
    own packages themselves - see models.Package's docstring)."""
    r = role(admin)
    if r == ROLE_SUPERADMIN:
        return None
    return {None, parent_admin_scope_id(admin)}
