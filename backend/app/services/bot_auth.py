"""Identity and authorization for /api/bot/* callers (design phase, 2026-09-27
audit - see docs/api-key-scope-audit-2026-09-27.md).

**Nothing in this file is called from routers/bot.py, deps.get_bot_api_key,
or telegram_bot/panel_bridge.py yet.** It exists to be reviewed and unit
tested on its own before any endpoint is wired to it (Product's explicit
instruction: design + characterization tests only, no enforcement, no
push). Importing this module has zero effect on any live request today.

The problem this designs around: every /api/bot/* caller today collapses
into ONE trust decision - "does deps.get_bot_api_key see a valid, enabled
ApiKey row" - and every endpoint after that reads whatever `owner_admin_id`
the CALLER chose to send, with nothing binding that claim to the key's own
identity (see the audit doc's endpoint table). Fixing that needs three
things together, not one:

  1. A key's IDENTITY is not just "has an owner or not" - a remote shared
     bot, a legacy unscoped key, a tenant's own integration, and a
     deliberate global integration are four different things that happen
     to look identical today. Collapsing them all into "owner_admin_id is
     NULL or isn't" was tried first and rejected (see KeyType below) -
     NULL would still mean four different things at once, which is the
     exact ambiguity that let this happen in the first place.
  2. Even a correctly-scoped key (tenant_integration, bound to one admin)
     should not automatically get every capability that admin's own bot
     has - a read-only reporting integration has no business calling
     add-balance or link-telegram just because it's honest about which
     tenant it belongs to.
  3. Whatever decides #1 and #2 has to be ONE function every endpoint goes
     through, in-process or over HTTP - not a per-endpoint copy of "does
     this owner_admin_id match", which is exactly how add_balance and
     link_telegram ended up with no check at all: two endpoints, two
     chances to forget.

BotPrincipal is the answer to all three: one small object that says who is
calling and what they may do, built once per request (HTTP: from the
ApiKey row; in-process: from telegram_bot/config.py's
config.bot_owner_admin_id threading.local) and then checked through the
same four helpers everywhere.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from . import hierarchy


class KeyType:
    """What KIND of caller a key represents - not just "has an owner or
    not". Migration Phase A stores this as a plain string column
    (ApiKey.key_type) rather than a DB enum, matching every other "mode"
    column in this project (xr_panel_mode, payment_card_mode, ...).

    LEGACY_GLOBAL     - every ApiKey row that exists before this migration
                        lands. Behaves EXACTLY as today: unscoped, full
                        capabilities, caller's owner_admin_id trusted as-is.
                        Never auto-upgraded - an admin has to deliberately
                        rotate a key into a scoped one.
    REMOTE_SHARED_BOT - minted by routers/remote_bot.py's deploy() for the
                        one remote-bot feature that exists today (see the
                        audit doc: it is ALWAYS the shared/global bot,
                        never a specific admin's). Unscoped like
                        LEGACY_GLOBAL, but distinguished so its lifecycle
                        (auto-disabled on the next deploy/on stop - already
                        implemented) is visibly tied to that one feature
                        rather than looking like an ordinary integration
                        key an admin made by hand.
    TENANT_INTEGRATION - a NEW key, created FOR one specific admin/seller.
                        owner_admin_id is mandatory at creation. Gets a
                        deliberately-chosen capability subset, never the
                        full set by default (see DEFAULT_CAPABILITIES).
    GLOBAL_INTEGRATION - a NEW key with no tenant, for a superadmin's own
                        cross-tenant tool. Requires a separate, warned,
                        password-confirmed creation step in the UI (Phase B)
                        - never the default choice.
    """
    LEGACY_GLOBAL = "legacy_global"
    REMOTE_SHARED_BOT = "remote_shared_bot"
    TENANT_INTEGRATION = "tenant_integration"
    GLOBAL_INTEGRATION = "global_integration"

    UNSCOPED = (LEGACY_GLOBAL, REMOTE_SHARED_BOT, GLOBAL_INTEGRATION)


# One capability per class of side effect an /api/bot/* endpoint can have -
# coarse on purpose (this is a first cut; splitting further is easy later,
# collapsing a too-fine split back down is a breaking change for whoever
# already has a key). Every endpoint in routers/bot.py maps to exactly one
# of these - see docs/api-key-scope-audit-2026-09-27.md's table for the
# mapping once Phase C actually wires require_bot_capability() in.
CUSTOMER_READ = "customer_read"    # list/get users, purchases, subscription link
CUSTOMER_WRITE = "customer_write"  # create/renew/reset/enable/delete users, connections
WALLET_WRITE = "wallet_write"      # add_balance
PAYMENT_READ = "payment_read"      # payment-info, payment-cards/{id}
PAYMENT_WRITE = "payment_write"    # record-payment
FILES_READ = "files_read"          # package/tutorial file+media downloads
BROADCAST = "broadcast"            # telegram-user-ids (bulk messaging recipient list)
ADMIN_LOOKUP = "admin_lookup"      # admin-by-telegram, admin-username

ALL_CAPABILITIES = frozenset({
    CUSTOMER_READ, CUSTOMER_WRITE, WALLET_WRITE, PAYMENT_READ,
    PAYMENT_WRITE, FILES_READ, BROADCAST, ADMIN_LOOKUP,
})

# What a NEW key of each type gets by default (Phase B - key creation UI).
# TENANT_INTEGRATION deliberately excludes wallet_write/payment_write/
# broadcast - a scoped integration has to be handed those explicitly, per
# Product's instruction ("نباید به‌صورت پیش‌فرض wallet_write، broadcast یا
# payment_write داشته باشد"). REMOTE_SHARED_BOT gets exactly what the
# interactive bot itself actually calls (see telegram_bot/remote_bridge.py's
# method list) - not the full set, so a leaked remote-bot key can't reach
# endpoints that bot never uses either.
DEFAULT_CAPABILITIES_BY_KEY_TYPE = {
    KeyType.LEGACY_GLOBAL: ALL_CAPABILITIES,
    KeyType.GLOBAL_INTEGRATION: ALL_CAPABILITIES,
    KeyType.REMOTE_SHARED_BOT: frozenset({
        CUSTOMER_READ, CUSTOMER_WRITE, WALLET_WRITE, PAYMENT_READ,
        PAYMENT_WRITE, FILES_READ, BROADCAST, ADMIN_LOOKUP,
    }),
    KeyType.TENANT_INTEGRATION: frozenset({CUSTOMER_READ, CUSTOMER_WRITE}),
}


@dataclass(frozen=True)
class BotPrincipal:
    """Who is calling /api/bot/*, and what they may do. Built once per
    request/call (see from_api_key/internal below) and threaded through
    the four require_bot_*/resolve_claimed_owner helpers - never
    reconstructed or second-guessed partway through a single request.

    key_id is None for an in-process caller (there is no ApiKey row at all
    - see telegram_bot/panel_bridge.py's module docstring: it calls
    routers/bot.py's Python functions directly, bypassing HTTP and this
    dependency entirely). label exists purely for logging/audit messages.
    """
    key_id: Optional[int]
    key_type: str
    owner_admin_id: Optional[int]
    capabilities: frozenset[str]
    label: str
    is_internal: bool = False

    @property
    def is_scoped(self) -> bool:
        """True only for a principal bound to one specific tenant - a
        TENANT_INTEGRATION key, or an in-process dedicated admin/seller
        bot. False for every unscoped shape (legacy, remote shared bot,
        global integration, and the in-process SHARED bot itself)."""
        return self.owner_admin_id is not None

    @classmethod
    def from_api_key(cls, key: models.ApiKey) -> "BotPrincipal":
        """The HTTP path - deps.get_bot_api_key would call this once it has
        already validated the key exists/is enabled (unchanged).

        Phase A has not landed yet: ApiKey has no key_type/owner_admin_id/
        capabilities columns today, so EVERY existing key maps to
        LEGACY_GLOBAL with owner_admin_id=None and every capability - which
        is the honest, literal truth about what every key can do right now
        (see the audit doc). Once Phase A's migration lands, this reads the
        real columns instead; no caller of from_api_key needs to change.
        """
        key_type = getattr(key, "key_type", None) or KeyType.LEGACY_GLOBAL
        owner_admin_id = getattr(key, "owner_admin_id", None)
        capabilities = getattr(key, "capabilities", None)
        if capabilities is None:
            capabilities = DEFAULT_CAPABILITIES_BY_KEY_TYPE.get(key_type, ALL_CAPABILITIES)
        else:
            capabilities = frozenset(capabilities)
        return cls(
            key_id=key.id, key_type=key_type, owner_admin_id=owner_admin_id,
            capabilities=capabilities, label=key.label,
        )

    @classmethod
    def internal(cls, owner_admin_id: Optional[int], *, label: str = "in-process bot") -> "BotPrincipal":
        """The in-process path - telegram_bot/panel_bridge.py builds one of
        these from config.bot_owner_admin_id (None for the shared bot, an
        AdminUser id for a dedicated admin/seller bot thread - see
        config.py's threading.local docstring) instead of ever touching
        deps.get_bot_api_key, since no HTTP request or ApiKey exists on
        this path at all.

        Full capabilities regardless of scope: this is this server's OWN
        bot code, not a third party - the isolation that matters here is
        WHICH tenant (owner_admin_id), never WHAT it may do within that
        tenant. is_internal=True lets the shared authorization helpers
        below tell "our own bot, scoped to one tenant" apart from "an
        external key that happens to be scoped", in case a future policy
        ever needs to (none does yet)."""
        return cls(
            key_id=None, key_type="internal", owner_admin_id=owner_admin_id,
            capabilities=ALL_CAPABILITIES, label=label, is_internal=True,
        )


# ------------------------------------------------------- central helpers
#
# Every endpoint that today does `owner_admin_id: Optional[int] = None` and
# hands it straight to _visibility_filter/_get_user_or_404 should, once
# Phase C actually wires this in, call resolve_claimed_owner() with that
# same value FIRST and use ITS return value instead - never the raw
# parameter. That is the one change that turns "every endpoint decides for
# itself" into "one function decides, every endpoint just asks it".

def resolve_claimed_owner(principal: BotPrincipal, claimed_owner_id: Optional[int]) -> Optional[int]:
    """What owner_admin_id this request may actually act as, given what the
    caller CLAIMED (the owner_admin_id query/payload value every scoped
    endpoint accepts today).

    - Unscoped principal (legacy/remote-shared/global-integration key, or
      the in-process SHARED bot): the claim is trusted as-is - this is
      EXACTLY today's behavior for every key that exists right now, so
      turning this function on changes nothing for any of them.
    - Scoped principal (tenant_integration key, or an in-process dedicated
      admin/seller bot): the claim must be empty (defaults to the
      principal's own tenant) or exactly match it - anything else is a
      cross-tenant attempt and is refused, not silently overridden. Made
      into a real 403 here rather than a "TODO" precisely so its behavior
      is proven by this module's own tests before Phase C ever calls it
      from a live request.
    """
    if not principal.is_scoped:
        return claimed_owner_id
    if claimed_owner_id is not None and claimed_owner_id != principal.owner_admin_id:
        raise HTTPException(
            403,
            f"این کلید/ربات فقط برای تنانت خودش (owner_admin_id={principal.owner_admin_id}) مجاز است",
        )
    return principal.owner_admin_id


def require_bot_capability(principal: BotPrincipal, capability: str) -> None:
    """Raises 403 unless `capability` is one of the ones this principal was
    granted. Endpoint-agnostic on purpose - add_balance calling
    require_bot_capability(principal, WALLET_WRITE) is the whole fix for
    "add_balance has no scope parameter at all": the capability check
    doesn't need one, it only needs the principal, which every endpoint
    will have regardless of whether IT personally remembers an
    owner_admin_id parameter."""
    if capability not in principal.capabilities:
        raise HTTPException(
            403,
            f"این کلید اجازه‌ی «{capability}» را ندارد (کلید: {principal.label})",
        )


def require_bot_user_access(db: Session, principal: BotPrincipal, user: models.User) -> None:
    """Raises 404 (not 403 - matching _get_user_or_404's existing
    convention of not confirming a username exists to a caller who can't
    see it) unless `principal` may act on `user`.

    Unscoped principals see everyone, exactly like today. A scoped
    principal is checked with hierarchy.can_see_user - the SAME function
    the admin panel's own JWT-authenticated routes use for a level-2
    Admin/level-3 Seller, so a scoped bot key/dedicated bot never sees more
    than that tenant's own web session would."""
    if not principal.is_scoped:
        return
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    if admin is None or not hierarchy.can_see_user(
        admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id
    ):
        raise HTTPException(404, "کاربر پیدا نشد")


def require_bot_node_access(db: Session, principal: BotPrincipal, node: models.Node) -> None:
    """Raises 404 unless `principal` may use `node` - mirrors
    hierarchy.accessible_node_ids exactly (see routers/nodes.py's own
    list_nodes for the identical pattern on the panel-web side), so a
    scoped bot never reaches a node its tenant's own web session couldn't.

    Deliberately NOT wired into GET /api/bot/nodes yet - see the audit
    doc's "تصمیم درباره /nodes" section. That endpoint's fix needs its own
    design pass first (in particular: what a SHARED bot's simple/plain-
    package purchase flow should show, since it currently relies on seeing
    every node to let a customer pick one by hand - see
    telegram_bot/handlers/customer.py's pick_node). This helper exists so
    that follow-up design has something ready to call once it's settled,
    not to be called from anywhere today."""
    if not principal.is_scoped:
        return
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    if admin is None:
        raise HTTPException(404, "نود پیدا نشد")
    allowed = hierarchy.accessible_node_ids(db, admin)
    if allowed is not None and node.id not in allowed:
        raise HTTPException(404, "نود پیدا نشد")


# ------------------------------------------------------------- key hashing
#
# Phase A/D design (not wired to any column yet - ApiKey.key_hash does not
# exist until the migration lands). SHA-256, not bcrypt: a generated API
# key is already high-entropy random data (see generate_api_key below), not
# a human-chosen password - the whole reason bcrypt/scrypt/argon2 deliberately
# cost CPU time is to slow down guessing a LOW-entropy secret, which does
# not apply here and would only slow down every single /api/bot/* request's
# auth check for no benefit. A deterministic, fast hash keyed for lookup
# (indexed equality, same as every other unique column in this project) is
# the right tool - hashed so a stolen database backup doesn't hand out
# working credentials directly, not to resist an online guessing attack a
# fast hash can't stop anyway (routers/bot.py's ip_guard-adjacent counters
# handle that side).

def hash_api_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_api_key(raw_key: str, key_hash: str) -> bool:
    """Constant-time compare, same reasoning as ip_guard's own token check -
    the response time of a near-miss must not leak how many leading
    characters were right."""
    return hmac.compare_digest(hash_api_key(raw_key), key_hash)
