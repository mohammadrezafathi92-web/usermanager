"""Principal-aware resource accessors for /api/bot/* - the closed set Phase C
(docs/api-key-scope-audit-2026-09-27.md, نسخه‌ی هشتم بخش ۳/۵) requires: every
place routers/bot.py or telegram_bot/panel_bridge.py touches a User, Package,
Tutorial, PaymentCard, Node, or AdminUser row goes through exactly one of the
11 functions below - never a raw db.query()/db.get() on one of those models
directly. tests/test_bot_resource_accessor_ast.py enforces this with an AST
scan of both callers AND of this file itself (so a 12th function can't be
added here under a plausible-sounding name either).

Both the HTTP router and the in-process bridge (telegram_bot/panel_bridge.py)
call these same functions with their own BotPrincipal - this is the single
place the "bypass" bug class (panel_bridge.py doing its own SessionLocal()+
db.query() for a handful of methods, found during Phase C's review rounds)
gets closed for good, by removing any OTHER way to reach these models.

Every accessor is scoped by its principal via bot_auth.py's central helpers
- require_bot_user_access, require_bot_resource_owner_access, or (for the
node-list case) selling_scope_node_ids. Every one of those is a no-op for an
unscoped principal (today's behavior, unchanged) and only actually restricts
once scope_enforced is true on the underlying row - which no row has yet in
Phase C's C0 stage."""
from __future__ import annotations

import os
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Query, Session, joinedload

from .. import models
from . import hierarchy
from .bot_auth import (
    BotPrincipal,
    _admin_hierarchy_allowed_ids,
    require_bot_node_access,
    require_bot_resource_owner_access,
    require_bot_user_access,
    resolve_claimed_owner,
)


# ------------------------------------------------------------------------- User

def _get_user_or_403(
    db: Session, principal: BotPrincipal, username: str, claimed_owner_admin_id: Optional[int],
) -> models.User:
    """Single-user lookup by username. claimed_owner_admin_id is mandatory
    (no default, matching _list_users_query's own convention below) - the
    exact owner_admin_id value the endpoint itself received (already
    defaulted by telegram_bot/panel_bridge.py's _scope() for in-process
    callers), or explicitly None for the few endpoints (link_telegram,
    add_balance, apply_referral) that have never taken one - never
    silently omitted.

    Applies TWO checks, not one - found missing during Phase C review
    (docs/api-key-scope-audit-2026-09-27.md): an earlier version of this
    accessor dropped the claim entirely and relied on require_bot_user_
    access alone, which is a no-op for every unscoped principal - i.e.
    every dedicated bot today, since dedicated_bot_scope_enforced still
    defaults False. That was a REAL regression, not a harmless no-op: the
    OLD (pre-Phase-C) _get_user_or_404 applied its owner_admin_id-based
    hierarchy.can_see_user check UNCONDITIONALLY whenever a caller passed
    one - which panel_bridge.py's _scope() always did for a dedicated
    bot's own calls, completely independent of any scope_enforced concept
    (that flag didn't exist yet). Dropping it meant Admin A's own
    dedicated bot could read/mutate Admin B's customers by username the
    moment this file shipped, C0 flags or not.

    1. resolve_claimed_owner + an unconditional hierarchy.can_see_user
       check against whatever the claim resolves to - restores exactly
       that old, always-on guarantee (mirrors _list_users_query's own
       _visibility_clause below, which already got this right for the
       list case).
    2. require_bot_user_access - the NEW principal-identity-bound check,
       still a no-op today for the same unscoped case, but the one that
       matters once scope_enforced is actually true on a row."""
    user = db.query(models.User).filter(models.User.username == username).first()
    if user is None:
        raise HTTPException(404, "کاربر پیدا نشد")
    owner_admin_id = resolve_claimed_owner(db, principal, claimed_owner_admin_id, endpoint="_get_user_or_403")
    if owner_admin_id is not None:
        admin = db.get(models.AdminUser, owner_admin_id)
        if admin is None or not hierarchy.can_see_user(
            admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id
        ):
            raise HTTPException(404, "کاربر پیدا نشد")
    require_bot_user_access(db, principal, user, endpoint="_get_user_or_403")
    return user


def _list_users_query(
    db: Session, principal: BotPrincipal, claimed_owner_admin_id: Optional[int], *,
    search: Optional[str] = None, telegram_id: Optional[int] = None,
) -> Query:
    """Filtered User query for list_users/get_user_by_telegram/
    list_users_by_telegram/get_sales_stats/telegram_user_ids - every one of
    them is the same underlying operation ("which Users may this claim
    see"), differing only in what the caller chains onto the returned Query
    (.first()/.all()/.count()/.with_entities(...).distinct()) and whether
    it's narrowed by telegram_id/search. claimed_owner_admin_id is
    mandatory (no default) - it must be the exact owner_admin_id value the
    endpoint itself received (already defaulted by telegram_bot/
    panel_bridge.py's _scope() for in-process callers), never silently
    omitted.

    The visibility clause itself (formerly routers/bot.py's own
    _visibility_filter, moved here and inlined as a nested closure rather
    than a routers/bot.py-level function so it counts as neither a 12th
    top-level accessor here nor a stray db.get(AdminUser, ...) outside the
    11 accessors there) is: None (unfiltered) for owner_admin_id=None - the
    shared/unowned bot legitimately serves every Admin's customers -
    otherwise hierarchy.user_visibility_clause for the account that
    resolves to, or an impossible clause for an owner_admin_id that
    resolves to no account at all (never fail open on an unknown/stale
    id)."""
    owner_admin_id = resolve_claimed_owner(db, principal, claimed_owner_admin_id, endpoint="_list_users_query")

    def _visibility_clause():
        if owner_admin_id is None:
            return None
        admin = db.get(models.AdminUser, owner_admin_id)
        if admin is None:
            from sqlalchemy import false
            return false()
        return hierarchy.user_visibility_clause(db, admin)

    query = db.query(models.User)
    if telegram_id is not None:
        query = query.filter(models.User.telegram_id == telegram_id)
    if search:
        like = f"%{search}%"
        query = query.filter(
            (models.User.username.ilike(like)) | (models.User.full_name.ilike(like))
        )
    clause = _visibility_clause()
    if clause is not None:
        query = query.filter(clause)
    return query


# ---------------------------------------------------------------------- Package

def _get_package_or_403(db: Session, principal: BotPrincipal, package_id: int) -> models.Package:
    package = db.get(models.Package, package_id)
    if package is None:
        raise HTTPException(404, "پکیج پیدا نشد")
    require_bot_resource_owner_access(
        principal, package.owner_admin_id,
        lambda p: hierarchy.accessible_package_owner_ids(db.get(models.AdminUser, p.owner_admin_id)),
        endpoint="_get_package_or_403",
    )
    return package


def _list_packages_query(db: Session, principal: BotPrincipal, claimed_owner_admin_id: Optional[int]) -> Query:
    """Same owner/target resolution routers/bot.py's list_packages has used
    since 2026-07-19 (two-way isolation - see
    hierarchy.accessible_package_owner_ids's docstring), just fed from
    resolve_claimed_owner's result instead of the raw claim directly."""
    owner_admin_id = resolve_claimed_owner(db, principal, claimed_owner_admin_id, endpoint="_list_packages_query")
    query = (
        db.query(models.Package)
        .options(
            joinedload(models.Package.connections),
            joinedload(models.Package.files),
            joinedload(models.Package.ovpn_templates),
        )
        .filter(models.Package.bot_enabled == True)  # noqa: E712
    )
    target = db.get(models.AdminUser, owner_admin_id) if owner_admin_id is not None else None
    if target is not None and not target.is_superadmin:
        allowed = hierarchy.accessible_package_owner_ids(target)
        query = query.filter(hierarchy.owner_id_in_clause(models.Package.owner_admin_id, allowed))
    else:
        query = query.filter(models.Package.owner_admin_id.is_(None))
    return query


# --------------------------------------------------------------------- Tutorial

def _get_tutorial_or_403(db: Session, principal: BotPrincipal, tutorial_id: int) -> models.Tutorial:
    tutorial = db.get(models.Tutorial, tutorial_id)
    if tutorial is None:
        raise HTTPException(404, "آموزش پیدا نشد")
    require_bot_resource_owner_access(
        principal, tutorial.owner_admin_id,
        lambda p: hierarchy.accessible_tutorial_owner_ids(db.get(models.AdminUser, p.owner_admin_id)),
        endpoint="_get_tutorial_or_403",
    )
    return tutorial


def _list_tutorials_query(db: Session, principal: BotPrincipal, claimed_owner_admin_id: Optional[int]) -> Query:
    owner_admin_id = resolve_claimed_owner(db, principal, claimed_owner_admin_id, endpoint="_list_tutorials_query")
    target = db.get(models.AdminUser, owner_admin_id) if owner_admin_id is not None else None
    if target is not None and not target.is_superadmin:
        allowed = hierarchy.accessible_tutorial_owner_ids(target)
        owner_filter = hierarchy.owner_id_in_clause(models.Tutorial.owner_admin_id, allowed)
    else:
        owner_filter = models.Tutorial.owner_admin_id.is_(None)
    return (
        db.query(models.Tutorial)
        .options(joinedload(models.Tutorial.media), joinedload(models.Tutorial.software))
        .filter(models.Tutorial.enabled == True, owner_filter)  # noqa: E712
    )


# ------------------------------------------------------------------- PaymentCard

def _get_payment_card_or_403(db: Session, principal: BotPrincipal, card_id: int) -> models.PaymentCard:
    card = db.get(models.PaymentCard, card_id)
    if card is None:
        raise HTTPException(404, "کارت پیدا نشد")

    def _allowed_owner_ids(p: BotPrincipal) -> set:
        """require_bot_resource_owner_access only ever calls this for a
        scoped principal (real decision) or a shadow copy of one (which
        _shadow_would_allow only builds when principal.owner_admin_id is
        already set) - so p.owner_admin_id is always a real id here,
        never None; db.get is safe unconditionally.

        Deliberately does NOT include None (the global card pool) even
        though an unscoped principal sees it fine (via
        require_bot_resource_owner_access's own is_scoped=False short-
        circuit, before this closure is even called) - a SCOPED tenant
        must never read/use-count the global pool, matching
        get_payment_info's own deliberate no-fallback-to-global-card rule
        for a tenant with a dedicated bot (see that function's docstring
        in routers/bot.py: "WHERE THE MONEY GOES does not fall back")."""
        admin = db.get(models.AdminUser, p.owner_admin_id)
        return hierarchy.owned_admin_ids(db, admin) if admin else set()

    require_bot_resource_owner_access(
        principal, card.owner_admin_id, _allowed_owner_ids, endpoint="_get_payment_card_or_403",
    )
    return card


# -------------------------------------------------------------------------- Node

def _get_node_or_403(db: Session, principal: BotPrincipal, node_id: int) -> models.Node:
    node = db.get(models.Node, node_id)
    if node is None:
        raise HTTPException(400, "نود پیدا نشد")
    require_bot_node_access(db, principal, node, endpoint="_get_node_or_403")
    return node


def _scoped_node_ids(db: Session, principal: BotPrincipal) -> Optional[set]:
    """Filter for GET /api/bot/nodes - None (unrestricted) for an unscoped
    principal, exactly today's full-list behavior; hierarchy.
    selling_scope_node_ids(db, admin) for a scoped one, so a scoped
    Seller's own dedicated bot still gets their parent Admin's sellable
    nodes rather than an always-empty administrative set."""
    if not principal.is_scoped:
        return None
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    return hierarchy.selling_scope_node_ids(db, admin) if admin else set()


# --------------------------------------------------------------------- AdminUser

def _get_admin_by_telegram_or_403(db: Session, principal: BotPrincipal, telegram_id: int) -> Optional[models.AdminUser]:
    """admin-by-telegram / panel_bridge.get_admin_by_telegram - returns None
    (not 404) for "no such admin", matching the original endpoint's own
    404-vs-None split (the HTTP layer raises 404, panel_bridge's own
    caller, admin_scope.py, treats a 404 ApiError as "not an admin")."""
    admin = db.query(models.AdminUser).filter(models.AdminUser.telegram_id == telegram_id).first()
    if admin is None:
        return None
    require_bot_resource_owner_access(
        principal, admin.id, _admin_hierarchy_allowed_ids(db),
        endpoint="_get_admin_by_telegram_or_403",
    )
    return admin


def _get_admin_or_403(db: Session, principal: BotPrincipal, admin_id: int) -> models.AdminUser:
    admin = db.get(models.AdminUser, admin_id)
    if admin is None:
        raise HTTPException(404, "ادمین پیدا نشد")
    require_bot_resource_owner_access(
        principal, admin.id, _admin_hierarchy_allowed_ids(db),
        endpoint="_get_admin_or_403",
    )
    return admin
