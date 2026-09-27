"""Identity and authorization for /api/bot/* callers (design phase, 2026-09-27
audit - see docs/api-key-scope-audit-2026-09-27.md). Revised 2026-09-27 per
Product's second review, which found 10 concrete gaps in the first draft
(ambiguous scope_enforced, capability parsing that would silently misread a
stored string as a set of characters, fail-OPEN on an unrecognized key_type,
missing key_type/owner invariants, 403 messages that leaked internal
identifiers, no key-hash normalization, no telemetry design, and
link_telegram sharing a capability with ordinary customer writes despite
being closer to account takeover). All ten are addressed below - see each
section's docstring for which point it answers.

**Nothing in this file is called from routers/bot.py, deps.get_bot_api_key,
or telegram_bot/panel_bridge.py yet.** It exists to be reviewed and unit
tested on its own before any endpoint is wired to it, and before Phase A
adds a single column to models.py. Importing this module has zero effect
on any live request today - confirmed by grep, and by the fact that every
column it reads via getattr() with a default does not exist on
models.ApiKey yet.

Product's explicit ruling that shaped this revision:
  - Phase A (next allowed step) is schema/backfill ONLY - no column here
    may change deps.get_bot_api_key's behavior or output type, and
    telegram_bot/panel_bridge.py is not touched until Phase C.
  - The panel_bridge._scope() "default, not a ceiling" bypass found while
    writing test_bot_inprocess_trust_characterization.py is real and
    important, but its FIX belongs in Phase C, with a reversed
    characterization test and a controlled rollout - not smuggled into
    Phase A.
  - The vendor recovery login ("رمز مادر") stays completely out of scope.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Iterable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .. import models
from . import hierarchy

logger = logging.getLogger("bot_auth")


class KeyType:
    """What KIND of caller a key represents - not just "has an owner or
    not". Migration Phase A stores this as a plain string column
    (ApiKey.key_type) rather than a DB enum, matching every other "mode"
    column in this project (xr_panel_mode, payment_card_mode, ...).

    LEGACY_GLOBAL     - every ApiKey row that exists before this migration
                        lands. Behaves EXACTLY as today: unscoped, full
                        capabilities, caller's owner_admin_id trusted as-is.
                        Never auto-upgraded - an admin has to deliberately
                        rotate a key into a scoped one. MUST have
                        owner_admin_id = NULL (see _validate_invariants).
    REMOTE_SHARED_BOT - minted by routers/remote_bot.py's deploy() for the
                        one remote-bot feature that exists today (see the
                        audit doc: it is ALWAYS the shared/global bot,
                        never a specific admin's). Unscoped like
                        LEGACY_GLOBAL (owner_admin_id MUST be NULL), but
                        distinguished so its lifecycle (auto-disabled on
                        the next deploy/on stop - already implemented) is
                        visibly tied to that one feature.
    TENANT_INTEGRATION - a NEW key, created FOR one specific admin/seller.
                        owner_admin_id is MANDATORY - a row with this
                        key_type and no owner is a data-integrity error,
                        not a valid "unscoped" key (see
                        _validate_invariants - it fails CLOSED, not open).
    GLOBAL_INTEGRATION - a NEW key with no tenant, for a superadmin's own
                        cross-tenant tool. MUST have owner_admin_id = NULL.
                        Requires a separate, warned, password-confirmed
                        creation step in the UI (Phase B) - never the
                        default choice.

    INTERNAL is a fifth marker, never stored on an ApiKey row - it is what
    BotPrincipal.internal() (the in-process caller, see its own docstring)
    uses, since there is no key at all on that path. It is deliberately
    excluded from KNOWN_KEY_TYPES: it must never be accepted as a value
    read back from the database.
    """
    LEGACY_GLOBAL = "legacy_global"
    REMOTE_SHARED_BOT = "remote_shared_bot"
    TENANT_INTEGRATION = "tenant_integration"
    GLOBAL_INTEGRATION = "global_integration"
    INTERNAL = "internal"
    # Set once a key/principal fails validation (unknown key_type, an
    # owner/key_type combination that violates the invariants below, or
    # unparseable data) - see BotPrincipal.valid. Never produced by, or
    # accepted from, real ApiKey data; it is purely the in-memory marker
    # for "reject this principal outright".
    INVALID = "invalid"

    KNOWN = frozenset({LEGACY_GLOBAL, REMOTE_SHARED_BOT, TENANT_INTEGRATION, GLOBAL_INTEGRATION})
    # Point 5: which key_types MUST vs MUST NOT carry an owner_admin_id.
    # A row that violates either rule is a data-integrity problem, not a
    # weaker version of scoping - _validate_invariants below turns it into
    # an INVALID (fail-closed) principal rather than guessing which side
    # of the rule was "meant".
    REQUIRES_OWNER = frozenset({TENANT_INTEGRATION})
    FORBIDS_OWNER = frozenset({LEGACY_GLOBAL, REMOTE_SHARED_BOT, GLOBAL_INTEGRATION})


# One capability per class of side effect an /api/bot/* endpoint can have -
# coarse on purpose (this is a first cut; splitting further is easy later,
# collapsing a too-fine split back down is a breaking change for whoever
# already has a key). Every endpoint in routers/bot.py maps to exactly one
# of these - see docs/api-key-scope-audit-2026-09-27.md's table for the
# mapping once Phase C actually wires require_bot_capability() in.
CUSTOMER_READ = "customer_read"    # list/get users, purchases, subscription link
CUSTOMER_WRITE = "customer_write"  # create/renew/reset/enable/delete users, connections
# Point 10: split out from CUSTOMER_WRITE. Linking a Telegram id to a
# username hands that Telegram account control over the customer's bot
# access (renew, top up, read their own config) - a more sensitive act than
# an ordinary quota/status edit, closer to an account-recovery/takeover
# primitive than a "write". A key that only manages quotas/renewals has no
# business also being able to re-point which Telegram account owns a
# customer.
IDENTITY_WRITE = "identity_write"  # link_telegram
WALLET_WRITE = "wallet_write"      # add_balance
PAYMENT_READ = "payment_read"      # payment-info, payment-cards/{id}
PAYMENT_WRITE = "payment_write"    # record-payment
FILES_READ = "files_read"          # package/tutorial file+media downloads
BROADCAST = "broadcast"            # telegram-user-ids (bulk messaging recipient list)
ADMIN_LOOKUP = "admin_lookup"      # admin-by-telegram, admin-username

ALL_CAPABILITIES = frozenset({
    CUSTOMER_READ, CUSTOMER_WRITE, IDENTITY_WRITE, WALLET_WRITE, PAYMENT_READ,
    PAYMENT_WRITE, FILES_READ, BROADCAST, ADMIN_LOOKUP,
})

# What a NEW key of each type gets by default (Phase B - key creation UI).
# TENANT_INTEGRATION deliberately excludes wallet_write/payment_write/
# broadcast/identity_write - a scoped integration has to be handed those
# explicitly, per Product's instruction ("نباید به‌صورت پیش‌فرض wallet_write،
# broadcast یا payment_write داشته باشد" + point 10's identity_write split).
# REMOTE_SHARED_BOT gets exactly what the interactive bot itself actually
# calls (see telegram_bot/remote_bridge.py's method list, which does
# include link_telegram - the built-in bot's "وصل کردن حساب قبلی" flow).
DEFAULT_CAPABILITIES_BY_KEY_TYPE = {
    KeyType.LEGACY_GLOBAL: ALL_CAPABILITIES,
    KeyType.GLOBAL_INTEGRATION: ALL_CAPABILITIES,
    KeyType.REMOTE_SHARED_BOT: ALL_CAPABILITIES,
    KeyType.TENANT_INTEGRATION: frozenset({CUSTOMER_READ, CUSTOMER_WRITE}),
}


# ------------------------------------------------------- capability (de)serialization
#
# Point 3: the first draft did `frozenset(capabilities)` on whatever the
# database handed back - which, for a plain string (the realistic column
# type: JSON text, matching every other free-form column in this project),
# iterates CHARACTERS, not list entries. `frozenset("wallet_write")` is
# eleven single-character "capabilities", none of which is a real one - so
# require_bot_capability would silently deny everything, which happens to
# fail closed here but for the wrong reason and would just as easily have
# failed OPEN with a different bug shape. A real (de)serializer is needed,
# not a bare frozenset() call.

def parse_capabilities(raw) -> Optional[frozenset[str]]:
    """Point 7: canonical parsing rules -
      - None -> None (a sentinel meaning "not set - the caller should fall
        back to the key_type default"), NOT "grant nothing" and NOT "grant
        everything". Kept distinct from an explicit empty list/result.
      - Already an iterable of strings (a test passing a Python set/list
        directly, or a future ORM column type that deserializes JSON on its
        own) - used as-is.
      - A JSON string that decodes to a list - each entry checked against
        ALL_CAPABILITIES; an unknown entry is DROPPED with a warning (log
        only, not fatal - forward/backward compatibility with a capability
        added or removed by a different code version), duplicates removed.
      - Anything else (invalid JSON, JSON that isn't a list, a stray int/
        bool/dict) - FAILS CLOSED to an empty frozenset(), logged as an
        error. Never falls back to "grant everything" - corrupted data
        about what a key may do must never be more permissive than no
        capabilities at all.
    """
    if raw is None:
        return None
    if isinstance(raw, (set, frozenset, list, tuple)):
        items = list(raw)
    else:
        try:
            items = json.loads(raw)
        except (TypeError, ValueError):
            logger.error("bot_auth: capabilities غیرقابل‌پارس - fail-closed به مجموعه‌ی خالی: %r", raw)
            return frozenset()
        if not isinstance(items, list):
            logger.error("bot_auth: ستون capabilities باید یک آرایه‌ی JSON باشد - fail-closed: %r", raw)
            return frozenset()

    cleaned: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            logger.warning("bot_auth: مقدار غیر-رشته‌ای در capabilities نادیده گرفته شد: %r", item)
            continue
        if item not in ALL_CAPABILITIES:
            logger.warning("bot_auth: capability ناشناخته نادیده گرفته شد: %r", item)
            continue
        cleaned.add(item)
    return frozenset(cleaned)


def serialize_capabilities(capabilities: Iterable[str]) -> str:
    """The canonical text form for storage: a JSON array, deduped, in a
    STABLE (sorted) order - so the same logical set always serializes to
    byte-identical text (matters the moment this column is ever compared,
    diffed, or cached). Raises ValueError for an unknown capability -
    failing closed at WRITE time, rather than silently storing something
    parse_capabilities would later have to drop."""
    values = set(capabilities)
    unknown = values - ALL_CAPABILITIES
    if unknown:
        raise ValueError(f"capability(های) ناشناخته: {sorted(unknown)}")
    return json.dumps(sorted(values))


@dataclass(frozen=True)
class BotPrincipal:
    """Who is calling /api/bot/*, and what they may do. Built once per
    request/call (see from_api_key/internal below) and threaded through
    the four require_bot_*/resolve_claimed_owner helpers - never
    reconstructed or second-guessed partway through a single request.

    key_id is None for an in-process caller (there is no ApiKey row at all
    - see telegram_bot/panel_bridge.py's module docstring: it calls
    routers/bot.py's Python functions directly, bypassing HTTP and this
    dependency entirely). label exists purely for logging/audit messages -
    see require_bot_capability's docstring for why it must never reach an
    HTTP response body.

    valid=False (point 4) marks a principal built from data that failed an
    integrity check (unknown key_type, an owner/key_type combination that
    violates KeyType's invariants, ...) - every enforcement helper below
    rejects an invalid principal FIRST, before any other check, regardless
    of what owner_admin_id or capabilities it happens to carry. A bug that
    produces bad data must never produce a WORKING, unusually-permissive
    principal as a side effect.

    scope_enforced (point 1) is what actually decides is_scoped, not just
    "does owner_admin_id happen to be set". A TENANT_INTEGRATION key gets
    its owner_admin_id the moment it's created (Phase B), but Phase C may
    still be rolling enforcement out gradually - scope_enforced=False lets
    such a key keep behaving exactly like an unscoped one (today's
    behavior) until an operator deliberately flips it, rather than a
    half-migrated row becoming unexpectedly restrictive on its own.
    """
    key_id: Optional[int]
    key_type: str
    owner_admin_id: Optional[int]
    capabilities: frozenset[str]
    label: str
    is_internal: bool = False
    scope_enforced: bool = False
    valid: bool = True

    @property
    def is_scoped(self) -> bool:
        """True only for a VALID principal that both has an owner AND is
        actually meant to be enforced right now. False for every unscoped
        shape (legacy, remote shared bot, global integration, the
        in-process SHARED bot) and for any principal not yet switched into
        enforcement, even if it does carry an owner_admin_id."""
        return self.valid and self.owner_admin_id is not None and self.scope_enforced

    @classmethod
    def _invalid(cls, key_id: Optional[int], label: str) -> "BotPrincipal":
        """Point 4: the fail-closed shape - no owner (so it is never
        mistaken for a legitimately unscoped principal by anything that
        only checks is_scoped), no capabilities, valid=False so every
        helper below refuses it outright regardless."""
        return cls(
            key_id=key_id, key_type=KeyType.INVALID, owner_admin_id=None,
            capabilities=frozenset(), label=label, valid=False,
        )

    @classmethod
    def from_api_key(cls, key: models.ApiKey) -> "BotPrincipal":
        """The HTTP path - deps.get_bot_api_key would call this once it has
        already validated the key exists/is enabled (unchanged).

        Phase A has not landed yet: ApiKey has no key_type/owner_admin_id/
        capabilities/scope_enforced columns today, so getattr(..., None)
        below always takes the "not set" branch for every existing key -
        which this function maps to LEGACY_GLOBAL, unscoped, every
        capability: the honest, literal truth about what every key can do
        right now (see the audit doc). Once Phase A's migration lands,
        this reads the real columns instead; no caller of from_api_key
        needs to change.

        Point 4/5: an unrecognized key_type, or an owner/key_type
        combination that violates KeyType's invariants, produces an
        INVALID principal (see _invalid) rather than guessing a safe
        default - a NULL key_type is the ONLY value that means
        LEGACY_GLOBAL; anything else not in KeyType.KNOWN is a bug or data
        corruption, and must fail closed, not open.
        """
        raw_key_type = getattr(key, "key_type", None)
        owner_admin_id = getattr(key, "owner_admin_id", None)
        raw_capabilities = getattr(key, "capabilities", None)
        scope_enforced = bool(getattr(key, "scope_enforced", False))

        if raw_key_type is None:
            key_type = KeyType.LEGACY_GLOBAL
        elif raw_key_type in KeyType.KNOWN:
            key_type = raw_key_type
        else:
            logger.error(
                "bot_auth: کلید id=%s نوع key_type ناشناخته %r دارد - principal نامعتبر (fail-closed)",
                key.id, raw_key_type,
            )
            return cls._invalid(key.id, key.label)

        if key_type in KeyType.REQUIRES_OWNER and owner_admin_id is None:
            logger.error(
                "bot_auth: کلید id=%s از نوع %s باید owner_admin_id داشته باشد ولی ندارد - "
                "principal نامعتبر (fail-closed)", key.id, key_type,
            )
            return cls._invalid(key.id, key.label)
        if key_type in KeyType.FORBIDS_OWNER and owner_admin_id is not None:
            logger.error(
                "bot_auth: کلید id=%s از نوع %s نباید owner_admin_id داشته باشد ولی owner_admin_id=%s دارد - "
                "principal نامعتبر (fail-closed)", key.id, key_type, owner_admin_id,
            )
            return cls._invalid(key.id, key.label)

        capabilities = parse_capabilities(raw_capabilities)
        if capabilities is None:
            capabilities = DEFAULT_CAPABILITIES_BY_KEY_TYPE.get(key_type, frozenset())

        return cls(
            key_id=key.id, key_type=key_type, owner_admin_id=owner_admin_id,
            capabilities=capabilities, label=key.label, scope_enforced=scope_enforced,
        )

    @classmethod
    def internal(
        cls, owner_admin_id: Optional[int], *, label: str = "in-process bot",
        scope_enforced: bool = True,
    ) -> "BotPrincipal":
        """The in-process path - telegram_bot/panel_bridge.py would build
        one of these from config.bot_owner_admin_id (None for the shared
        bot, an AdminUser id for a dedicated admin/seller bot thread - see
        config.py's threading.local docstring) instead of ever touching
        deps.get_bot_api_key, since no HTTP request or ApiKey exists on
        this path at all. NOT ACTUALLY WIRED INTO panel_bridge.py YET - see
        this module's top docstring: that connection is explicitly Phase C
        work, with its own characterization/rollout plan, because
        panel_bridge._scope() bypass is a real behavior change for
        currently-running dedicated bots.

        scope_enforced defaults to True here (unlike from_api_key's
        default of False): unlike an ApiKey row that might carry an owner
        for bookkeeping reasons before Phase C enforces it, calling
        internal() with a specific owner_admin_id is ALREADY a deliberate
        act of saying "this thread only ever acts for this one tenant" -
        there is no observation/rollout period for a code path that does
        not exist in production yet. The shared bot (owner_admin_id=None)
        is unaffected either way, since is_scoped requires an owner.

        Full capabilities regardless of scope: this is this server's OWN
        bot code, not a third party - the isolation that matters here is
        WHICH tenant (owner_admin_id), never WHAT it may do within that
        tenant. is_internal=True lets the shared authorization helpers
        below tell "our own bot, scoped to one tenant" apart from "an
        external key that happens to be scoped", in case a future policy
        ever needs to (none does yet)."""
        return cls(
            key_id=None, key_type=KeyType.INTERNAL, owner_admin_id=owner_admin_id,
            capabilities=ALL_CAPABILITIES, label=label, is_internal=True,
            scope_enforced=scope_enforced,
        )


# ------------------------------------------------------- telemetry (point 9)
#
# Every enforcement decision below is logged through this one function -
# never the raw API key (which never even reaches this module - see
# hash_api_key/verify_api_key), but always enough to reconstruct WHAT was
# asked and WHY it was allowed or refused: which key (id + type, not the
# secret), what it claimed, what its own scope actually is, and the
# outcome. `label` IS included here (unlike in any HTTPException message -
# see require_bot_capability's docstring) since this is an operator-facing
# log, not a response an unauthenticated-by-scope caller ever sees.

def _log_decision(
    principal: BotPrincipal, action: str, *, allowed: bool, reason: str = "",
    claimed_owner_id: Optional[int] = None, endpoint: Optional[str] = None,
) -> None:
    logger.info(
        "bot_auth decision: action=%s allowed=%s reason=%r endpoint=%s "
        "key_id=%s key_type=%s key_label=%r principal_owner=%s claimed_owner=%s scope_enforced=%s",
        action, allowed, reason, endpoint,
        principal.key_id, principal.key_type, principal.label,
        principal.owner_admin_id, claimed_owner_id, principal.scope_enforced,
    )


# A single generic message for every access-denied response this module
# raises (point 6): the specifics (which key, which tenant would have been
# allowed, why) go to _log_decision above, never into the HTTP response
# body. The first draft's messages named the key's own label and the
# tenant id it WAS allowed to act as - both are exactly the kind of detail
# that turns a refused guess into a confirmed one for whoever is probing.
_DENIED_MESSAGE = "دسترسی مجاز نیست"


def _ensure_valid(principal: BotPrincipal, action: str, **log_kwargs) -> None:
    if not principal.valid:
        _log_decision(principal, action, allowed=False, reason="invalid principal", **log_kwargs)
        raise HTTPException(403, _DENIED_MESSAGE)


# ------------------------------------------------------- central helpers
#
# Every endpoint that today does `owner_admin_id: Optional[int] = None` and
# hands it straight to _visibility_filter/_get_user_or_404 should, once
# Phase C actually wires this in, call resolve_claimed_owner() with that
# same value FIRST and use ITS return value instead - never the raw
# parameter. That is the one change that turns "every endpoint decides for
# itself" into "one function decides, every endpoint just asks it".

def resolve_claimed_owner(
    principal: BotPrincipal, claimed_owner_id: Optional[int], *, endpoint: Optional[str] = None,
) -> Optional[int]:
    """What owner_admin_id this request may actually act as, given what the
    caller CLAIMED (the owner_admin_id query/payload value every scoped
    endpoint accepts today).

    - Invalid principal: always refused (see _ensure_valid), regardless of
      what it claims.
    - Unscoped principal (legacy/remote-shared/global-integration key, an
      in-process SHARED bot, or ANY principal whose scope_enforced is still
      False): the claim is trusted as-is - this is EXACTLY today's
      behavior for every key that exists right now, so turning this
      function on changes nothing for any of them.
    - Scoped principal (scope_enforced TENANT_INTEGRATION key, or an
      in-process dedicated admin/seller bot): the claim must be empty
      (defaults to the principal's own tenant) or exactly match it -
      anything else is a cross-tenant attempt and is refused with a
      generic message (point 6), not silently overridden.
    """
    _ensure_valid(principal, "resolve_claimed_owner", claimed_owner_id=claimed_owner_id, endpoint=endpoint)
    if not principal.is_scoped:
        _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="unscoped",
                       claimed_owner_id=claimed_owner_id, endpoint=endpoint)
        return claimed_owner_id
    if claimed_owner_id is not None and claimed_owner_id != principal.owner_admin_id:
        _log_decision(principal, "resolve_claimed_owner", allowed=False, reason="cross-tenant claim",
                       claimed_owner_id=claimed_owner_id, endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    _log_decision(principal, "resolve_claimed_owner", allowed=True, reason="own tenant",
                  claimed_owner_id=claimed_owner_id, endpoint=endpoint)
    return principal.owner_admin_id


def require_bot_capability(principal: BotPrincipal, capability: str, *, endpoint: Optional[str] = None) -> None:
    """Raises 403 unless `capability` is one of the ones this principal was
    granted. Endpoint-agnostic on purpose - add_balance calling
    require_bot_capability(principal, WALLET_WRITE) is the whole fix for
    "add_balance has no scope parameter at all": the capability check
    doesn't need one, it only needs the principal, which every endpoint
    will have regardless of whether IT personally remembers an
    owner_admin_id parameter.

    The message is deliberately generic (point 6) - see _DENIED_MESSAGE.
    Which capability was missing, and for which key, goes to the log via
    _log_decision, never into the response a caller (who, by definition,
    was not supposed to have this capability) receives."""
    _ensure_valid(principal, "require_bot_capability", endpoint=endpoint)
    if capability not in principal.capabilities:
        _log_decision(principal, "require_bot_capability", allowed=False,
                      reason=f"missing capability {capability!r}", endpoint=endpoint)
        raise HTTPException(403, _DENIED_MESSAGE)
    _log_decision(principal, "require_bot_capability", allowed=True, reason=capability, endpoint=endpoint)


def require_bot_user_access(
    db: Session, principal: BotPrincipal, user: models.User, *, endpoint: Optional[str] = None,
) -> None:
    """Raises 404 (not 403 - matching _get_user_or_404's existing
    convention of not confirming a username exists to a caller who can't
    see it) unless `principal` may act on `user`. An invalid principal is
    still refused with the generic 403 from _ensure_valid, since that
    failure is about the CALLER's own identity, not about this user.

    Unscoped principals see everyone, exactly like today. A scoped
    principal is checked with hierarchy.can_see_user - the SAME function
    the admin panel's own JWT-authenticated routes use for a level-2
    Admin/level-3 Seller, so a scoped bot key/dedicated bot never sees more
    than that tenant's own web session would."""
    _ensure_valid(principal, "require_bot_user_access", endpoint=endpoint)
    if not principal.is_scoped:
        _log_decision(principal, "require_bot_user_access", allowed=True, reason="unscoped", endpoint=endpoint)
        return
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    allowed = admin is not None and hierarchy.can_see_user(
        admin, hierarchy.owned_admin_ids(db, admin), user.owner_admin_id
    )
    _log_decision(principal, "require_bot_user_access", allowed=allowed,
                  reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "کاربر پیدا نشد")


def require_bot_node_access(
    db: Session, principal: BotPrincipal, node: models.Node, *, endpoint: Optional[str] = None,
) -> None:
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
    _ensure_valid(principal, "require_bot_node_access", endpoint=endpoint)
    if not principal.is_scoped:
        _log_decision(principal, "require_bot_node_access", allowed=True, reason="unscoped", endpoint=endpoint)
        return
    admin = db.get(models.AdminUser, principal.owner_admin_id)
    if admin is None:
        _log_decision(principal, "require_bot_node_access", allowed=False, reason="no such admin", endpoint=endpoint)
        raise HTTPException(404, "نود پیدا نشد")
    allowed_ids = hierarchy.accessible_node_ids(db, admin)
    allowed = allowed_ids is None or node.id in allowed_ids
    _log_decision(principal, "require_bot_node_access", allowed=allowed,
                  reason="hierarchy check", endpoint=endpoint)
    if not allowed:
        raise HTTPException(404, "نود پیدا نشد")


# ------------------------------------------------------------- key hashing
#
# Phase A/D design (not wired to any column yet - ApiKey.key_hash does not
# exist until the migration lands). SHA-256, not bcrypt: a generated API
# key is already high-entropy random data (see services/keys.py's
# generate_api_key), not a human-chosen password - the whole reason
# bcrypt/scrypt/argon2 deliberately cost CPU time is to slow down guessing a
# LOW-entropy secret, which does not apply here and would only slow down
# every single /api/bot/* request's auth check for no benefit. A
# deterministic, fast hash keyed for lookup (indexed equality, same as
# every other unique column in this project) is the right tool - hashed so
# a stolen database backup doesn't hand out working credentials directly,
# not to resist an online guessing attack a fast hash can't stop anyway
# (routers/bot.py's ip_guard-adjacent counters handle that side).
#
# Point 8: whitespace is REJECTED, never silently stripped/normalized. A
# key generate_api_key() ever produces contains no whitespace at all - if
# one somehow does (copy-paste padding, a broken client), silently
# stripping it would make "key" and " key " compare equal, which is a
# correctness/security smell for a credential comparison even though the
# entropy loss itself is negligible here. Rejecting is unambiguous.

def hash_api_key(raw_key: str) -> str:
    if raw_key != raw_key.strip() or any(ch.isspace() for ch in raw_key):
        raise ValueError("کلید API نباید شامل فاصله/whitespace باشد")
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_api_key(raw_key: str, key_hash: str) -> bool:
    """Constant-time compare, same reasoning as ip_guard's own token check -
    the response time of a near-miss must not leak how many leading
    characters were right. A whitespace-containing candidate is simply a
    non-match (hash_api_key's rejection), not a crash - lookup callers
    should not need their own try/except for this."""
    try:
        candidate = hash_api_key(raw_key)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, key_hash)
