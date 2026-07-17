"""Central rules for the 3-tier reseller hierarchy: superadmin -> admin
(level 2, full panel access scoped to their own tree) -> seller (level 3,
granular-permission scoped, works within their parent admin's packages/
nodes). Fixed at exactly 3 levels - see AdminUser.parent_admin_id's
docstring. Every router that needs to scope a query by "which admin(s) can
see this" should go through the helpers here instead of re-deriving the
same role/ownership logic locally, the same way permissions.py centralizes
effective_permissions()."""
from __future__ import annotations

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
    """Which AdminUser ids' USERS this account may see/manage.
    None = unrestricted (superadmin - sees everyone).
    An Admin (level 2) sees their own users AND every one of their
    Sellers' users (roll-up oversight of their whole tree).
    A Seller (level 3) only ever sees their own."""
    r = role(admin)
    if r == ROLE_SUPERADMIN:
        return None
    if r == ROLE_SELLER:
        return {admin.id}
    seller_ids = [
        row.id
        for row in db.query(models.AdminUser.id).filter(models.AdminUser.parent_admin_id == admin.id).all()
    ]
    return {admin.id, *seller_ids}


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
