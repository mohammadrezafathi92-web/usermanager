"""services/bot_auth.py - unit tests (2026-09-27 audit). Phase C's C0 stage
(docs/api-key-scope-audit-2026-09-27.md, نسخه‌ی هشتم) wires this module into
routers/bot.py and telegram_bot/panel_bridge.py, but every enforcing branch
still stays inert (scope_enforced defaults to False everywhere) until a
later, separate rollout stage flips one row at a time - see
test_bot_route_policy_coverage.py/test_bot_resource_accessor_ast.py for the
wiring-is-complete side of that guarantee, and
test_bot_inprocess_trust_characterization.py for the in-process trust
question. This file covers bot_auth.py's own design in isolation: the
BotPrincipal/key_type/owner invariants, capability parsing, the five
central require_bot_*/resolve_claimed_owner functions (now hierarchy-aware,
not exact-match, and each with a shadow-evaluation branch),
NodeAuthorizationScope's three resolvers, and key hashing.

Run:  python3 backend/tests/test_bot_auth_principal.py
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker

from app import models
from app.services import bot_auth, hierarchy

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def raises_403(fn):
    try:
        fn()
        return False
    except HTTPException as exc:
        return exc.status_code == 403


def raises_404(fn):
    try:
        fn()
        return False
    except HTTPException as exc:
        return exc.status_code == 404


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_admin(db, username, *, superadmin=False, role=None, parent=None):
    row = models.AdminUser(
        username=username, hashed_password="x", is_superadmin=superadmin, role=role,
        parent_admin_id=parent.id if parent else None,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def add_user(db, username, owner):
    row = models.User(username=username, owner_admin_id=owner.id if owner else None)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def fake_key(id=1, label="k", key_type=None, owner_admin_id=None, capabilities=None, scope_enforced=False):
    """A stand-in for models.ApiKey with Phase-A columns that don't exist
    yet - from_api_key() reads them via getattr(..., None/False), so a
    plain namespace with only the attributes under test is enough and
    keeps these tests from depending on a migration that hasn't landed."""
    return SimpleNamespace(
        id=id, label=label, key_type=key_type, owner_admin_id=owner_admin_id,
        capabilities=capabilities, scope_enforced=scope_enforced,
    )


print("=" * 60)
print("--- capability (de)serialization (point 3 & 7) ---")
print("=" * 60)

check("None means 'not set' - distinct from an explicit empty result",
      bot_auth.parse_capabilities(None), None)
check("a real JSON array parses correctly",
      bot_auth.parse_capabilities(json.dumps([bot_auth.CUSTOMER_READ, bot_auth.WALLET_WRITE])),
      frozenset({bot_auth.CUSTOMER_READ, bot_auth.WALLET_WRITE}))
check("a Python list/set is accepted as-is, not re-parsed as JSON",
      bot_auth.parse_capabilities([bot_auth.CUSTOMER_READ]), frozenset({bot_auth.CUSTOMER_READ}))
check("duplicates in the source are collapsed",
      bot_auth.parse_capabilities(json.dumps([bot_auth.CUSTOMER_READ, bot_auth.CUSTOMER_READ])),
      frozenset({bot_auth.CUSTOMER_READ}))
check("an unknown capability string is dropped, not fatal, and does not "
      "poison the rest of the list",
      bot_auth.parse_capabilities(json.dumps(["not_a_real_capability", bot_auth.CUSTOMER_READ])),
      frozenset({bot_auth.CUSTOMER_READ}))
check("a plain string being iterated CHARACTER BY CHARACTER never happens - "
      "the exact bug this rewrite fixes: a bare word that fails JSON "
      "parsing fails CLOSED to empty, not to a set of one-letter garbage",
      bot_auth.parse_capabilities("wallet_write"), frozenset())
check("invalid JSON fails closed to an empty set, never to 'everything'",
      bot_auth.parse_capabilities("{not valid json"), frozenset())
check("JSON that parses but isn't a list (e.g. an object) fails closed too",
      bot_auth.parse_capabilities(json.dumps({"a": 1})), frozenset())
check("an explicit empty list parses to an empty set (not None - this IS a "
      "deliberate 'no capabilities' answer, distinct from 'not set')",
      bot_auth.parse_capabilities("[]"), frozenset())
check("a non-string item inside an otherwise-valid list is skipped",
      bot_auth.parse_capabilities(json.dumps([bot_auth.CUSTOMER_READ, 123, None])),
      frozenset({bot_auth.CUSTOMER_READ}))

print("\n--- serialize_capabilities: stable order + reject-unknown-at-write-time ---")
s1 = bot_auth.serialize_capabilities({bot_auth.WALLET_WRITE, bot_auth.CUSTOMER_READ})
s2 = bot_auth.serialize_capabilities({bot_auth.CUSTOMER_READ, bot_auth.WALLET_WRITE})
check("order is stable regardless of input set iteration order", s1, s2)
check("round-trips through parse_capabilities cleanly",
      bot_auth.parse_capabilities(s1), frozenset({bot_auth.WALLET_WRITE, bot_auth.CUSTOMER_READ}))
try:
    bot_auth.serialize_capabilities({"totally_made_up"})
    check("serializing an unknown capability raises ValueError (fail-closed at write time)", False, True)
except ValueError:
    check("serializing an unknown capability raises ValueError (fail-closed at write time)", True, True)

print("\n" + "=" * 60)
print("--- BotPrincipal.from_api_key(): today's (Phase-A-not-yet-landed) mapping ---")
print("=" * 60)

p = bot_auth.BotPrincipal.from_api_key(fake_key())
check("a key with no columns set at all maps to LEGACY_GLOBAL (NULL key_type "
      "is the only value that means legacy)", p.key_type, bot_auth.KeyType.LEGACY_GLOBAL)
check("...with no owner (matches today's real unscoped behavior exactly)", p.owner_admin_id, None)
check("...and every capability (nothing restricts a legacy key today)",
      p.capabilities, bot_auth.ALL_CAPABILITIES)
check("is_scoped is False for an unscoped principal", p.is_scoped, False)
check("is_internal is False for an HTTP-derived principal", p.is_internal, False)
check("valid is True for well-formed data", p.valid, True)

print("\n--- point 4: an UNKNOWN key_type fails closed, not open ---")
bad = bot_auth.BotPrincipal.from_api_key(fake_key(key_type="something_from_a_future_version"))
check("an unrecognized key_type produces an INVALID principal", bad.valid, False)
check("...with no capabilities at all", bad.capabilities, frozenset())
check("...and require_bot_capability refuses EVERYTHING for it, not just "
      "the specific capability that would have been checked",
      raises_403(lambda: bot_auth.require_bot_capability(bad, bot_auth.CUSTOMER_READ)), True)

print("\n--- point 5: key_type/owner invariants ---")
missing_owner = bot_auth.BotPrincipal.from_api_key(
    fake_key(key_type=bot_auth.KeyType.TENANT_INTEGRATION, owner_admin_id=None))
check("tenant_integration with NO owner is invalid, not silently unscoped",
      missing_owner.valid, False)

unexpected_owner = bot_auth.BotPrincipal.from_api_key(
    fake_key(key_type=bot_auth.KeyType.LEGACY_GLOBAL, owner_admin_id=42))
check("legacy_global with an owner set anyway is invalid, not silently scoped",
      unexpected_owner.valid, False)

unexpected_owner2 = bot_auth.BotPrincipal.from_api_key(
    fake_key(key_type=bot_auth.KeyType.GLOBAL_INTEGRATION, owner_admin_id=1))
check("global_integration with an owner set is invalid too", unexpected_owner2.valid, False)

well_formed_tenant = bot_auth.BotPrincipal.from_api_key(
    fake_key(key_type=bot_auth.KeyType.TENANT_INTEGRATION, owner_admin_id=7, scope_enforced=True))
check("a correctly-formed tenant_integration key IS valid", well_formed_tenant.valid, True)
check("...and IS scoped once scope_enforced is true", well_formed_tenant.is_scoped, True)
check("...with the restrictive default capabilities (no wallet/broadcast/payment/identity)",
      well_formed_tenant.capabilities, bot_auth.DEFAULT_CAPABILITIES_BY_KEY_TYPE[bot_auth.KeyType.TENANT_INTEGRATION])

print("\n--- point 1 / Phase C invariant: scope_enforced gates is_scoped for OTHER key_types, "
      "but tenant_integration+scope_enforced=false is INVALID, never silently unscoped ---")
tenant_not_enforced = bot_auth.BotPrincipal.from_api_key(
    fake_key(key_type=bot_auth.KeyType.TENANT_INTEGRATION, owner_admin_id=7, scope_enforced=False))
check("a tenant_integration key with scope_enforced=False is INVALID - Phase C's own "
      "invariant (docs/api-key-scope-audit-2026-09-27.md, نسخه‌ی هشتم بخش ۱): this key_type "
      "already carries real capabilities, so 'unscoped' would mean cross-tenant writes, "
      "strictly worse than refusing outright. Phase B's own activate_tenant_key endpoint "
      "only ever sets enabled=true and scope_enforced=true together, in one UPDATE - this "
      "row shape should never occur from that path, but a bug/manual edit must still fail "
      "closed, not fail open.",
      tenant_not_enforced.valid, False)
check("...with zero capabilities as a result", tenant_not_enforced.capabilities, frozenset())

print("\n" + "=" * 60)
print("--- BotPrincipal.internal(): the in-process (panel_bridge.py/miniapp.py) shape ---")
print("=" * 60)

shared_bot = bot_auth.BotPrincipal.internal(None)
check("the SHARED bot's principal has no owner (matches config.bot_owner_admin_id=None)",
      shared_bot.owner_admin_id, None)
check("...is not scoped", shared_bot.is_scoped, False)
check("...but IS internal", shared_bot.is_internal, True)
check("...and has every capability, plus the two receipt-approval ones that only a panel-managed bot ever holds",
      shared_bot.capabilities, bot_auth.ALL_CAPABILITIES | bot_auth.MANAGED_BOT_CAPABILITIES)

# Real hierarchy fixtures from here on (not a magic id like 42) - resolve_
# claimed_owner is hierarchy-aware now (point below) and needs real
# AdminUser rows to resolve owned_admin_ids against.
db2 = make_db()
sa = add_admin(db2, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add_admin(db2, "admin1", role=hierarchy.ROLE_ADMIN)
a2 = add_admin(db2, "admin2", role=hierarchy.ROLE_ADMIN)
s1 = add_admin(db2, "seller1", role=hierarchy.ROLE_SELLER, parent=a1)
s_of_superadmin = add_admin(db2, "seller_of_root", role=hierarchy.ROLE_SELLER, parent=sa)
a1_cust = add_user(db2, "a1_cust", a1)
a2_cust = add_user(db2, "a2_cust", a2)
orphan = add_user(db2, "orphan_cust", None)

dedicated_bot = bot_auth.BotPrincipal.internal(a1.id)
check("a DEDICATED admin bot's principal carries that admin's id", dedicated_bot.owner_admin_id, a1.id)
check("...and IS scoped BY DEFAULT (internal() defaults scope_enforced=True - "
      "unlike from_api_key, there is no half-migrated row to protect here)",
      dedicated_bot.is_scoped, True)

dedicated_not_enforced = bot_auth.BotPrincipal.internal(a1.id, scope_enforced=False)
check("internal() still allows an explicit scope_enforced=False override if a "
      "future Phase C rollout needs one", dedicated_not_enforced.is_scoped, False)

print("\n" + "=" * 60)
print("--- resolve_claimed_owner(): today's trust behavior is preserved for unscoped principals ---")
print("=" * 60)

check("an unscoped (legacy/global) principal: whatever the caller claims wins, unchanged",
      bot_auth.resolve_claimed_owner(db2, p, 999), 999)
check("...even when the claim is None (today's most common case)",
      bot_auth.resolve_claimed_owner(db2, p, None), None)
check("the SHARED in-process bot behaves identically - unscoped means unscoped "
      "regardless of HTTP vs in-process",
      bot_auth.resolve_claimed_owner(db2, shared_bot, 7), 7)

print("\n--- resolve_claimed_owner(): a SCOPED principal cannot be redirected to another tenant ---")
check("no claim at all defaults to the principal's own tenant",
      bot_auth.resolve_claimed_owner(db2, dedicated_bot, None), a1.id)
check("a claim that matches the principal's own tenant is accepted",
      bot_auth.resolve_claimed_owner(db2, dedicated_bot, a1.id), a1.id)
check("a claim for a DIFFERENT, unrelated tenant is refused with 403, not silently overridden",
      raises_403(lambda: bot_auth.resolve_claimed_owner(db2, dedicated_bot, a2.id)), True)

print("\n--- resolve_claimed_owner(): hierarchy-aware, not exact-match - an Admin may claim "
      "their own Seller's id (e.g. handlers/admin_pending.py's perform_approval, approving a "
      "receipt whose real owner is a Seller while the parent Admin completes the approval) ---")
check("a1 (Admin) claiming their own Seller's id (s1) is accepted - s1 is in a1's "
      "owned_admin_ids subtree",
      bot_auth.resolve_claimed_owner(db2, dedicated_bot, s1.id), s1.id)
scoped_as_seller_early = bot_auth.BotPrincipal.internal(s1.id)
check("...but a Seller claiming their PARENT Admin's id is refused - owned_admin_ids for a "
      "Seller is just {themselves}, never a roll-up the other direction",
      raises_403(lambda: bot_auth.resolve_claimed_owner(db2, scoped_as_seller_early, a1.id)), True)

print("\n--- point 6: the 403 raised carries no key label or allowed-owner detail ---")
try:
    bot_auth.resolve_claimed_owner(db2, dedicated_bot, a2.id)
    check("resolve_claimed_owner raised for a cross-tenant claim", False, True)
except HTTPException as exc:
    check("the exception detail is the generic message only", exc.detail, "دسترسی مجاز نیست")
    check("...it does not leak the principal's own tenant id",
          str(a1.id) in str(exc.detail), False)
try:
    bot_auth.require_bot_capability(
        bot_auth.BotPrincipal(key_id=9, key_type="x", owner_admin_id=None,
                               capabilities=frozenset(), label="a very identifying label", valid=True),
        bot_auth.WALLET_WRITE,
    )
    check("require_bot_capability raised for a missing capability", False, True)
except HTTPException as exc:
    check("...and its message doesn't leak the key's own label either",
          "identifying label" in str(exc.detail), False)

print("\n--- require_bot_capability(): point 10, identity_write is its own capability ---")
scoped_reader = bot_auth.BotPrincipal(
    key_id=1, key_type=bot_auth.KeyType.TENANT_INTEGRATION, owner_admin_id=42,
    capabilities=frozenset({bot_auth.CUSTOMER_READ, bot_auth.CUSTOMER_WRITE}),
    label="ordinary tenant key", scope_enforced=True,
)
check("a capability the key WAS granted passes",
      bot_auth.require_bot_capability(scoped_reader, bot_auth.CUSTOMER_READ), None)
check("a capability the key was NOT granted raises 403 - this alone is the entire "
      "fix for add_balance/link_telegram having no scope parameter: the check "
      "doesn't need one, only the principal",
      raises_403(lambda: bot_auth.require_bot_capability(scoped_reader, bot_auth.WALLET_WRITE)), True)
check("an ordinary customer_write key does NOT automatically get identity_write "
      "(link_telegram) - they are deliberately separate capabilities now",
      raises_403(lambda: bot_auth.require_bot_capability(scoped_reader, bot_auth.IDENTITY_WRITE)), True)

print("\n--- default capability sets match Product's explicit instructions ---")
tenant_defaults = bot_auth.DEFAULT_CAPABILITIES_BY_KEY_TYPE[bot_auth.KeyType.TENANT_INTEGRATION]
check("a brand-new tenant_integration key does NOT get wallet_write by default",
      bot_auth.WALLET_WRITE in tenant_defaults, False)
check("...nor broadcast", bot_auth.BROADCAST in tenant_defaults, False)
check("...nor payment_write", bot_auth.PAYMENT_WRITE in tenant_defaults, False)
check("...nor identity_write (link_telegram) - the new, more sensitive capability",
      bot_auth.IDENTITY_WRITE in tenant_defaults, False)
check("...but does get ordinary customer read/write (the whole point of the key existing)",
      {bot_auth.CUSTOMER_READ, bot_auth.CUSTOMER_WRITE} <= tenant_defaults, True)
remote_defaults = bot_auth.DEFAULT_CAPABILITIES_BY_KEY_TYPE[bot_auth.KeyType.REMOTE_SHARED_BOT]
check("the remote shared bot DOES get identity_write - the built-in bot's own "
      "«وصل کردن حساب قبلی» flow calls link_telegram",
      bot_auth.IDENTITY_WRITE in remote_defaults, True)

print("\n" + "=" * 60)
print("--- require_bot_user_access(): mirrors the panel's own hierarchy exactly ---")
print("=" * 60)

unscoped_principal = bot_auth.BotPrincipal.from_api_key(fake_key())
check("an unscoped principal may reach ANY tenant's customer - today's real behavior, unchanged",
      bot_auth.require_bot_user_access(db2, unscoped_principal, a2_cust), None)

scoped_as_a1 = bot_auth.BotPrincipal.internal(a1.id)
check("a1's own dedicated bot may reach its own customer",
      bot_auth.require_bot_user_access(db2, scoped_as_a1, a1_cust), None)
check("...but NOT admin2's customer - 404, matching _get_user_or_404's existing "
      "not-found convention rather than a 403 that would confirm the username exists",
      raises_404(lambda: bot_auth.require_bot_user_access(db2, scoped_as_a1, a2_cust)), True)
check("...and not an ownerless customer either - only a superadmin principal may reach those",
      raises_404(lambda: bot_auth.require_bot_user_access(db2, scoped_as_a1, orphan)), True)

scoped_as_superadmin = bot_auth.BotPrincipal.internal(sa.id)
check("a superadmin-scoped principal DOES reach an ownerless customer",
      bot_auth.require_bot_user_access(db2, scoped_as_superadmin, orphan), None)

invalid_principal = bot_auth.BotPrincipal.from_api_key(fake_key(key_type="garbage"))
check("an INVALID principal is refused with 403 before any hierarchy check runs at all - "
      "identity failures and resource-not-found failures are kept distinct",
      raises_403(lambda: bot_auth.require_bot_user_access(db2, invalid_principal, a1_cust)), True)

print("\n" + "=" * 60)
print("--- require_bot_node_access(): mirrors hierarchy.accessible_node_ids exactly ---")
print("=" * 60)

node_a1_owned = models.Node(name="a1-own", type=models.NodeType.mikrotik, enabled=True,
                             owner_admin_id=a1.id, mt_host="1.1.1.1", mt_username="u", mt_password="p")
node_granted_to_a1 = models.Node(name="granted-to-a1", type=models.NodeType.mikrotik, enabled=True,
                                  mt_host="2.2.2.2", mt_username="u", mt_password="p")
node_untouched = models.Node(name="unrelated", type=models.NodeType.mikrotik, enabled=True,
                              mt_host="3.3.3.3", mt_username="u", mt_password="p")
db2.add_all([node_a1_owned, node_granted_to_a1, node_untouched])
db2.commit()
db2.add(models.AdminNodeAccess(admin_id=a1.id, node_id=node_granted_to_a1.id))
db2.commit()

check("an unscoped principal reaches any node - today's real (undesigned-yet) behavior",
      bot_auth.require_bot_node_access(db2, unscoped_principal, node_untouched), None)
check("a1's own dedicated bot reaches a node IT owns",
      bot_auth.require_bot_node_access(db2, scoped_as_a1, node_a1_owned), None)
check("...reaches a node explicitly GRANTED to it",
      bot_auth.require_bot_node_access(db2, scoped_as_a1, node_granted_to_a1), None)
check("...but not an unrelated node it has no relation to",
      raises_404(lambda: bot_auth.require_bot_node_access(db2, scoped_as_a1, node_untouched)), True)

scoped_as_seller = bot_auth.BotPrincipal.internal(s1.id)  # s1 is a1's own Seller, created earlier
check("a Seller-scoped principal (s1, child of a1) reaches the SAME nodes its parent Admin "
      "does - via hierarchy.selling_scope_node_ids, NOT the raw accessible_node_ids (which "
      "would always be empty for a Seller) - a Seller resells their parent's infrastructure, "
      "so pick_node/manual-connection checks must resolve through the parent, not the Seller "
      "itself",
      bot_auth.require_bot_node_access(db2, scoped_as_seller, node_a1_owned), None)
check("...same for a node explicitly GRANTED to the parent Admin",
      bot_auth.require_bot_node_access(db2, scoped_as_seller, node_granted_to_a1), None)
check("...but still not a node unrelated to that parent either",
      raises_404(lambda: bot_auth.require_bot_node_access(db2, scoped_as_seller, node_untouched)), True)

scoped_as_seller_of_root = bot_auth.BotPrincipal.internal(s_of_superadmin.id)
check("a Seller whose parent IS the superadmin (a real, deliberately-supported "
      "configuration - hierarchy.validate_placement allows it explicitly, not corrupted "
      "data) does NOT get the superadmin's own unrestricted node access (accessible_node_ids "
      "for a superadmin is None/unrestricted, by design, since node infrastructure oversight "
      "is not isolated for them) - handing that straight to a Seller would open every node "
      "in the whole panel, a fail-open accident. selling_scope_node_ids fails closed to only "
      "explicitly-global nodes (Node.owner_admin_id IS NULL) instead, same as "
      "accessible_package_owner_ids' own translation for this exact configuration.",
      raises_404(lambda: bot_auth.require_bot_node_access(db2, scoped_as_seller_of_root, node_a1_owned)), True)
global_node = models.Node(name="global", type=models.NodeType.mikrotik, enabled=True,
                           mt_host="4.4.4.4", mt_username="u", mt_password="p")  # owner_admin_id=None
db2.add(global_node)
db2.commit()
check("...but DOES reach an explicitly-global (superadmin-owned) node",
      bot_auth.require_bot_node_access(db2, scoped_as_seller_of_root, global_node), None)

print("\n" + "=" * 60)
print("--- key hashing (Phase A/D design) ---")
print("=" * 60)

raw = "sk_live_deadbeefcafef00d"
h = bot_auth.hash_api_key(raw)
check("hashing is deterministic (needed for an indexed lookup column)",
      bot_auth.hash_api_key(raw), h)
check("a different key hashes to something else", bot_auth.hash_api_key(raw + "x") != h, True)
check("verify_api_key accepts the right key", bot_auth.verify_api_key(raw, h), True)
check("verify_api_key rejects a wrong key", bot_auth.verify_api_key("wrong", h), False)

print("\n--- point 8: whitespace is explicitly rejected, never silently normalized ---")
try:
    bot_auth.hash_api_key("  " + raw)
    check("hashing a leading-whitespace key raises", False, True)
except ValueError:
    check("hashing a leading-whitespace key raises", True, True)
try:
    bot_auth.hash_api_key(raw + "\n")
    check("hashing a trailing-newline key raises", False, True)
except ValueError:
    check("hashing a trailing-newline key raises", True, True)
check("verify_api_key on a whitespace-padded candidate is simply False, not a crash",
      bot_auth.verify_api_key(" " + raw + " ", h), False)

print("\n--- point 8: key_hash uniqueness is an enforceable DB constraint (design check) ---")
# A throw-away table shaped like the planned ApiKey.key_hash column
# (unique + indexed) - proves the CONSTRAINT this migration relies on
# actually rejects a collision, without touching the real models.py.
ScratchBase = declarative_base()


class _ScratchApiKey(ScratchBase):
    __tablename__ = "scratch_api_keys"
    id = Column(Integer, primary_key=True)
    key_hash = Column(String(64), unique=True, index=True, nullable=False)


scratch_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
ScratchBase.metadata.create_all(scratch_engine)
ScratchSession = sessionmaker(bind=scratch_engine)()
ScratchSession.add(_ScratchApiKey(key_hash=bot_auth.hash_api_key("key-one")))
ScratchSession.commit()
ScratchSession.add(_ScratchApiKey(key_hash=bot_auth.hash_api_key("key-two")))
ScratchSession.commit()
check("two DIFFERENT keys' hashes insert without conflict",
      ScratchSession.query(_ScratchApiKey).count(), 2)
ScratchSession.add(_ScratchApiKey(key_hash=bot_auth.hash_api_key("key-one")))
try:
    ScratchSession.commit()
    check("inserting the SAME key's hash twice is rejected by the unique constraint", False, True)
except IntegrityError:
    ScratchSession.rollback()
    check("inserting the SAME key's hash twice is rejected by the unique constraint", True, True)

print("\n" + "=" * 60)
print("--- point 9: telemetry logs the decision, never the raw key or an HTTP-visible detail ---")
print("=" * 60)

import logging  # noqa: E402


class _CaptureHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


capture = _CaptureHandler()
bot_auth.logger.addHandler(capture)
bot_auth.logger.setLevel(logging.INFO)
try:
    bot_auth.resolve_claimed_owner(db2, dedicated_bot, a2.id, endpoint="/api/bot/users/x/add-balance")
except HTTPException:
    pass
finally:
    bot_auth.logger.removeHandler(capture)

joined = "\n".join(capture.records)
check("the denial was logged", "allowed=False" in joined, True)
check("...with the endpoint recorded", "/api/bot/users/x/add-balance" in joined, True)
check("...with the key's own id recorded (for audit correlation)", "key_id=None" in joined, True)
check("...with the claimed owner recorded", f"claimed_owner={a2.id}" in joined, True)
check("the raw API key never appears anywhere in the log (there is no raw key on "
      "this principal to begin with - internal() never carries one)",
      "sk_live" in joined, False)

print("\n" + "=" * 60)
print("--- would_allow_if_enforced: shadow evaluation for an unscoped-but-owned principal ---")
print("=" * 60)

# Phase C's C0 stage: a principal with an owner but scope_enforced=False (a
# dedicated bot before its own dedicated_bot_scope_enforced flag is turned
# on) is still fully unscoped for the REAL decision - but each central
# function also logs what WOULD have happened if it were enforced, purely
# for observation, never affecting the real response.
capture2 = _CaptureHandler()
bot_auth.logger.addHandler(capture2)
bot_auth.logger.setLevel(logging.INFO)
result = bot_auth.resolve_claimed_owner(db2, dedicated_not_enforced, a2.id, endpoint="/api/bot/shadow-test")
bot_auth.logger.removeHandler(capture2)
joined2 = "\n".join(capture2.records)
check("the REAL decision is unaffected - claim trusted as-is, exactly like today",
      result, a2.id)
check("...but the log line still says the real decision was 'allowed'",
      "allowed=True" in joined2, True)
check("...and records what would have happened if enforced - a2 is NOT in dedicated_not_"
      "enforced's (a1's) owned_admin_ids, so this must say would_allow_if_enforced=False",
      "would_allow_if_enforced=False" in joined2, True)

capture3 = _CaptureHandler()
bot_auth.logger.addHandler(capture3)
bot_auth.logger.setLevel(logging.INFO)
bot_auth.resolve_claimed_owner(db2, dedicated_not_enforced, s1.id, endpoint="/api/bot/shadow-test-2")
bot_auth.logger.removeHandler(capture3)
joined3 = "\n".join(capture3.records)
check("a claim that WOULD be allowed once enforced (s1 is a1's own Seller) shows "
      "would_allow_if_enforced=True, even while completely unenforced today",
      "would_allow_if_enforced=True" in joined3, True)

check("a principal with NO owner at all has nothing to shadow-evaluate - "
      "_shadow_would_allow returns None outright, never attempts a hypothetical check",
      bot_auth._shadow_would_allow(shared_bot, lambda p: True), None)
check("a principal already scope_enforced=True has nothing hypothetical left to log either",
      bot_auth._shadow_would_allow(dedicated_bot, lambda p: True), None)


def _boom(_p):
    raise RuntimeError("a bug inside the shadow decision itself")


check("a bug INSIDE the shadow decision function is caught and reported as unknown (None) - "
      "never re-raised, since a broken shadow evaluation must not fail the real "
      "(already-decided) request it is only annotating the log line for",
      bot_auth._shadow_would_allow(dedicated_not_enforced, _boom), None)

print("\n" + "=" * 60)
print("--- NodeAuthorizationScope: closed type, no bare None floating around ---")
print("=" * 60)

unrestricted = bot_auth.NodeAuthorizationScope.unrestricted_scope()
check("unrestricted_scope() carries no admin_id", unrestricted.admin_id, None)
check("...and is flagged unrestricted", unrestricted.unrestricted, True)
scoped_scope = bot_auth.NodeAuthorizationScope.scoped(a1.id)
check("scoped() carries the real admin_id", scoped_scope.admin_id, a1.id)
check("...and is NOT flagged unrestricted", scoped_scope.unrestricted, False)

try:
    bot_auth.NodeAuthorizationScope(admin_id=a1.id, unrestricted=True)
    check("unrestricted=True with an admin_id is rejected at construction", False, True)
except ValueError:
    check("unrestricted=True with an admin_id is rejected at construction", True, True)
try:
    bot_auth.NodeAuthorizationScope(admin_id=None, unrestricted=False)
    check("unrestricted=False with no admin_id is rejected at construction", False, True)
except ValueError:
    check("unrestricted=False with no admin_id is rejected at construction", True, True)

print("\n--- resolve_package_authorization_scope(): a NULL-owned package stays unrestricted "
      "forever, regardless of who buys it ---")
global_pkg = models.Package(name="global pkg", quota_gb=1, duration_days=30, price=0, owner_admin_id=None)
tenant_pkg = models.Package(name="tenant pkg", quota_gb=1, duration_days=30, price=0, owner_admin_id=a1.id)
check("a NULL-owned (superadmin-built) package resolves to unrestricted",
      bot_auth.resolve_package_authorization_scope(global_pkg).unrestricted, True)
check("a tenant-owned package resolves to that tenant's own scope",
      bot_auth.resolve_package_authorization_scope(tenant_pkg).admin_id, a1.id)

print("\n--- resolve_bot_authorization_scope(): gated on is_scoped, and fail-closed on an "
      "invalid principal (never silently unrestricted just because it failed validation) ---")
check("an unscoped principal (C0's default for everyone) resolves to unrestricted - the "
      "manual/pick_node path's real behavior stays unchanged until scope_enforced flips",
      bot_auth.resolve_bot_authorization_scope(shared_bot).unrestricted, True)
check("a scoped principal resolves to its own tenant",
      bot_auth.resolve_bot_authorization_scope(dedicated_bot).admin_id, a1.id)
check("an INVALID principal raises (via _ensure_valid) rather than falling through to "
      "'not scoped, therefore unrestricted' - the exact ambient-authority failure this "
      "closed type exists to prevent",
      raises_403(lambda: bot_auth.resolve_bot_authorization_scope(invalid_principal)), True)

print("\n--- resolve_superadmin_actor_scope(): re-validates is_superadmin itself, never "
      "trusts that the caller already checked ---")
check("a genuine superadmin resolves to unrestricted",
      bot_auth.resolve_superadmin_actor_scope(sa).unrestricted, True)
try:
    bot_auth.resolve_superadmin_actor_scope(a1)
    check("a non-superadmin AdminUser is rejected, not silently granted unrestricted access", False, True)
except ValueError:
    check("a non-superadmin AdminUser is rejected, not silently granted unrestricted access", True, True)
try:
    bot_auth.resolve_superadmin_actor_scope(None)
    check("None is rejected too", False, True)
except ValueError:
    check("None is rejected too", True, True)

print("\n" + "=" * 60)
print("--- require_bot_resource_owner_access(): NULL means 'the superadmin's', never "
      "'unowned/open to everyone' - lazy allowed_owner_ids_fn, never crashes on an "
      "unscoped principal ---")
print("=" * 60)

check("an unscoped principal is allowed regardless of resource owner - allowed_owner_ids_fn "
      "is never even called, so it cannot crash on owner_admin_id=None (the common case)",
      bot_auth.require_bot_resource_owner_access(
          unscoped_principal, None, lambda p: (_ for _ in ()).throw(AssertionError("should not run"))),
      None)
check("a scoped principal reaching a resource whose owner is in its allowed set passes",
      bot_auth.require_bot_resource_owner_access(dedicated_bot, a1.id, lambda p: {a1.id, s1.id}), None)
check("...but not one outside it",
      raises_404(lambda: bot_auth.require_bot_resource_owner_access(dedicated_bot, a2.id, lambda p: {a1.id, s1.id})), True)

allowed_ids_fn = bot_auth._admin_hierarchy_allowed_ids(db2)
check("_admin_hierarchy_allowed_ids: a1 may resolve itself",
      a1.id in allowed_ids_fn(dedicated_bot), True)
check("...and its own Seller s1",
      s1.id in allowed_ids_fn(dedicated_bot), True)
check("...but not an unrelated admin (a2)",
      a2.id in allowed_ids_fn(dedicated_bot), False)

stale_principal = bot_auth.BotPrincipal.internal(999999, scope_enforced=True)  # no such AdminUser row
check("a principal pointing at a deleted/stale AdminUser row fails closed to an empty set - "
      "not an AttributeError from hierarchy.owned_admin_ids(db, None)",
      allowed_ids_fn(stale_principal), set())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
