"""services/bot_auth.py - design-phase unit tests (2026-09-27 audit, stage 3
continued). This module is NOT called from any live endpoint yet (see its
own module docstring) - these tests exist so the design (BotPrincipal, the
four require_bot_*/resolve_claimed_owner helpers, key hashing) is proven
correct in isolation BEFORE Product approves wiring it into routers/bot.py
or telegram_bot/panel_bridge.py. Nothing here touches production behavior.

Run:  python3 backend/tests/test_bot_auth_principal.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

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


def add_admin(db, username, *, superadmin=False, role=None):
    row = models.AdminUser(username=username, hashed_password="x", is_superadmin=superadmin, role=role)
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


print("=" * 60)
print("--- BotPrincipal.from_api_key(): today's (Phase-A-not-yet-landed) mapping ---")
print("=" * 60)

db = make_db()
plain_key = models.ApiKey(label="third-party", key="abc123")
db.add(plain_key)
db.commit()
db.refresh(plain_key)

p = bot_auth.BotPrincipal.from_api_key(plain_key)
check("every existing key maps to LEGACY_GLOBAL (no key_type column exists yet)",
      p.key_type, bot_auth.KeyType.LEGACY_GLOBAL)
check("...with no owner (matches today's real unscoped behavior exactly)", p.owner_admin_id, None)
check("...and every capability (nothing restricts a legacy key today)",
      p.capabilities, bot_auth.ALL_CAPABILITIES)
check("is_scoped is False for an unscoped principal", p.is_scoped, False)
check("is_internal is False for an HTTP-derived principal", p.is_internal, False)
check("key_id carries the real row id (for audit logging)", p.key_id, plain_key.id)

print("\n--- BotPrincipal.internal(): the in-process (panel_bridge.py) shape ---")
shared_bot = bot_auth.BotPrincipal.internal(None)
check("the SHARED bot's principal has no owner (matches config.bot_owner_admin_id=None)",
      shared_bot.owner_admin_id, None)
check("...is not scoped", shared_bot.is_scoped, False)
check("...but IS internal", shared_bot.is_internal, True)
check("...and has every capability (this server's own trusted code)",
      shared_bot.capabilities, bot_auth.ALL_CAPABILITIES)

dedicated_bot = bot_auth.BotPrincipal.internal(42)
check("a DEDICATED admin bot's principal carries that admin's id", dedicated_bot.owner_admin_id, 42)
check("...and IS scoped", dedicated_bot.is_scoped, True)

print("\n" + "=" * 60)
print("--- resolve_claimed_owner(): today's trust behavior is preserved for unscoped principals ---")
print("=" * 60)

check("an unscoped (legacy/global) principal: whatever the caller claims wins, unchanged",
      bot_auth.resolve_claimed_owner(p, 999), 999)
check("...even when the claim is None (today's most common case)",
      bot_auth.resolve_claimed_owner(p, None), None)
check("the SHARED in-process bot behaves identically - unscoped means unscoped "
      "regardless of HTTP vs in-process",
      bot_auth.resolve_claimed_owner(shared_bot, 7), 7)

print("\n--- resolve_claimed_owner(): a SCOPED principal cannot be redirected to another tenant ---")
check("no claim at all defaults to the principal's own tenant",
      bot_auth.resolve_claimed_owner(dedicated_bot, None), 42)
check("a claim that matches the principal's own tenant is accepted",
      bot_auth.resolve_claimed_owner(dedicated_bot, 42), 42)
check("a claim for a DIFFERENT tenant is refused with 403, not silently overridden",
      raises_403(lambda: bot_auth.resolve_claimed_owner(dedicated_bot, 999)), True)

print("\n" + "=" * 60)
print("--- require_bot_capability() ---")
print("=" * 60)

scoped_reader = bot_auth.BotPrincipal(
    key_id=1, key_type=bot_auth.KeyType.TENANT_INTEGRATION, owner_admin_id=42,
    capabilities=frozenset({bot_auth.CUSTOMER_READ}), label="read-only reporting key",
)
check("a capability the key WAS granted passes",
      bot_auth.require_bot_capability(scoped_reader, bot_auth.CUSTOMER_READ), None)
check("a capability the key was NOT granted raises 403 - this alone is the entire "
      "fix for add_balance/link_telegram having no scope parameter: the check "
      "doesn't need one, only the principal",
      raises_403(lambda: bot_auth.require_bot_capability(scoped_reader, bot_auth.WALLET_WRITE)), True)

print("\n--- default capability sets match Product's explicit instruction ---")
tenant_defaults = bot_auth.DEFAULT_CAPABILITIES_BY_KEY_TYPE[bot_auth.KeyType.TENANT_INTEGRATION]
check("a brand-new tenant_integration key does NOT get wallet_write by default",
      bot_auth.WALLET_WRITE in tenant_defaults, False)
check("...nor broadcast", bot_auth.BROADCAST in tenant_defaults, False)
check("...nor payment_write", bot_auth.PAYMENT_WRITE in tenant_defaults, False)
check("...but does get ordinary customer read/write (the whole point of the key existing)",
      {bot_auth.CUSTOMER_READ, bot_auth.CUSTOMER_WRITE} <= tenant_defaults, True)

print("\n" + "=" * 60)
print("--- require_bot_user_access(): mirrors the panel's own hierarchy exactly ---")
print("=" * 60)

db2 = make_db()
sa = add_admin(db2, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add_admin(db2, "admin1", role=hierarchy.ROLE_ADMIN)
a2 = add_admin(db2, "admin2", role=hierarchy.ROLE_ADMIN)
a1_cust = add_user(db2, "a1_cust", a1)
a2_cust = add_user(db2, "a2_cust", a2)
orphan = add_user(db2, "orphan_cust", None)

unscoped_principal = bot_auth.BotPrincipal.from_api_key(plain_key)
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

s1 = add_admin(db2, "seller1", role=hierarchy.ROLE_SELLER)
hierarchy.rebuild_path(db2, s1)
db2.commit()
scoped_as_seller = bot_auth.BotPrincipal.internal(s1.id)
check("a Seller-scoped principal reaches NO node directly - sellers never pick "
      "nodes themselves, only through their parent Admin's Packages, exactly like "
      "the panel-web side",
      raises_404(lambda: bot_auth.require_bot_node_access(db2, scoped_as_seller, node_untouched)), True)

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

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
