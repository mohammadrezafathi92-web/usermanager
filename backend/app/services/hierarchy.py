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

    NOTE: this is deliberately DIFFERENT from accessible_node_ids below,
    which stays unrestricted (None) for a superadmin - node infrastructure
    is still fully superadmin-administered. accessible_package_owner_ids
    USED to also stay unrestricted for a superadmin but no longer does
    (see its own docstring) - packages now follow the same walled-off
    rule as the customer roster here."""
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
    None = unrestricted (superadmin - sees/manages every node regardless of
    owner; node infrastructure oversight was deliberately NOT isolated like
    Package/Tutorial/DiscountCode, see this module's other accessible_*
    functions for the ones that were).
    An Admin (level 2) sees the UNION of two things: nodes explicitly
    granted via AdminNodeAccess (superadmin-owned infrastructure shared
    with them), AND nodes they've added themselves (Node.owner_admin_id ==
    their own id - see routers/nodes.py's create_node, added so an Admin
    can plug in their own server with their own IP/SSH credentials without
    waiting on a superadmin). A Seller (level 3) has NO direct node access
    at all - they only ever work through their parent Admin's already-built
    Packages, never pick a node by hand."""
    r = role(admin)
    if r == ROLE_SUPERADMIN:
        return None
    if r == ROLE_SELLER:
        return set()
    granted = {
        row.node_id
        for row in db.query(models.AdminNodeAccess.node_id).filter(models.AdminNodeAccess.admin_id == admin.id).all()
    }
    owned = {
        row.id
        for row in db.query(models.Node.id).filter(models.Node.owner_admin_id == admin.id).all()
    }
    return granted | owned


def accessible_package_owner_ids(admin: models.AdminUser) -> set[int | None]:
    """Which Package.owner_admin_id values this account may see/use - ALWAYS
    a concrete set now, never an "unrestricted" None sentinel (changed
    2026-07-19, see below). An Admin sees only their own; a Seller sees
    only their parent Admin's (Sellers never own packages themselves - see
    models.Package's docstring); a superadmin sees only their own "global"
    ones (owner_admin_id IS NULL, {None}).

    History: a superadmin used to see EVERY package regardless of owner
    (this function returned bare None, callers treated that as "skip the
    filter entirely"). Reported as a bug by the panel owner: a package
    created inside a level-3 Seller's tree showed up in the superadmin's
    own Packages page, fully editable/deletable/purchasable there too -
    inconsistent with every other per-tenant resource (Users, own bot, own
    backup, own payment info), which are ALL fully isolated in both
    directions (an Admin/Seller never sees the superadmin's global stuff,
    AND the superadmin never sees an Admin's/Seller's own stuff). Confirmed
    with the panel owner ("ایزوله کامل (مثل کاربران/بات/بک‌آپ)") that
    packages should follow the exact same two-way isolation rule - so a
    superadmin now gets the same treatment as everyone else: their OWN
    scope only ({None}, meaning owner_admin_id IS NULL), never anyone
    else's tree. See routers/bot.py's list_packages for the matching fix
    on the bot-facing side (the shared/global bot used to show every
    Admin's/Seller's bot_enabled packages too, for the same reason).

    NOTE for any caller building a raw query filter from this: always use
    owner_id_in_clause() below rather than a plain `.in_(...)` - the
    returned set may contain None (meaning "IS NULL"), and SQL's
    `IN (NULL, 1)` never matches a NULL column value (this already bit
    global packages once, see git history)."""
    if role(admin) == ROLE_SUPERADMIN:
        return {None}
    return {parent_admin_scope_id(admin)}


def accessible_tutorial_owner_ids(admin: models.AdminUser) -> set[int | None]:
    """Which Tutorial.owner_admin_id values this account may see - exact
    same shape/reasoning as accessible_package_owner_ids above (see its
    docstring), applied to tutorials (2026-07-19, confirmed with the panel
    owner: "لیست شخصی خودش" per superadmin/Admin, Sellers read-only from
    their parent's list). A Seller never authors tutorials themselves, so
    this only ever needs to resolve to their PARENT Admin's scope - same
    parent_admin_scope_id() used for packages."""
    if role(admin) == ROLE_SUPERADMIN:
        return {None}
    return {parent_admin_scope_id(admin)}


def accessible_discount_code_owner_ids(db: Session, admin: models.AdminUser) -> set[int | None]:
    """Which DiscountCode.owner_admin_id values this account may VIEW in
    the panel's Discount Codes list - NOT the same as who may EDIT a given
    code (see routers/discount_codes.py's _owns_discount_code for that,
    which is strictly narrower: only the code's actual owner, never a
    roll-up target).

    Unlike accessible_package_owner_ids/accessible_tutorial_owner_ids
    (where a Seller can never own the resource, so there's nothing to roll
    up), a level-3 Seller CAN own their own discount codes here - so a
    level-2 Admin additionally gets read-only oversight of their own
    Sellers' codes, exactly like owned_admin_ids's roll-up for the customer
    roster. A superadmin does NOT roll up to Admins'/Sellers' codes at all
    - confirmed explicitly with the panel owner ("سوپرادمین ... ایزوله")،
    matching every other per-tenant resource's superadmin isolation this
    session."""
    r = role(admin)
    if r == ROLE_SUPERADMIN:
        return {None}
    if r == ROLE_SELLER:
        return {admin.id}
    seller_ids = [
        row.id
        for row in db.query(models.AdminUser.id).filter(models.AdminUser.parent_admin_id == admin.id).all()
    ]
    return {admin.id, *seller_ids}


def owner_id_in_clause(column, allowed: set):
    """Correct SQLAlchemy translation of "column's value is one of `allowed`"
    when `allowed` may contain None meaning "IS NULL" (e.g. the set returned
    by accessible_package_owner_ids). Plain `column.in_(allowed)` silently
    never matches NULL rows even with None literally in the Python set -
    ANSI SQL's `IN (NULL, 1)` is never TRUE for `column IS NULL`."""
    non_null = {v for v in allowed if v is not None}
    parts = []
    if non_null:
        parts.append(column.in_(non_null))
    if None in allowed:
        parts.append(column.is_(None))
    if not parts:
        from sqlalchemy import false
        return false()
    if len(parts) == 1:
        return parts[0]
    return or_(*parts)
