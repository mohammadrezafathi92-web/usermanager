"""Phase A migration test (2026-09-27 API-key-scope audit, docs/api-key-
scope-audit-2026-09-27.md, services/bot_auth.py) - proves the additive
ApiKey columns and their backfill behave correctly in the three shapes
Product asked for: an OLD database upgrading, a FRESH database, and
STARTUP RUNNING AGAIN (idempotency).

Nothing here touches deps.get_bot_api_key, any router, or telegram_bot/
panel_bridge.py - this only exercises app.main's own
_auto_migrate_missing_columns/_backfill_api_key_hashes, the exact functions
a real startup calls, pointed at scratch databases (same technique already
established in test_package_groups.py's "upgrading a panel that already
has packages" section - reused here, not reinvented).

Run:  python3 backend/tests/test_api_key_migration.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import models
from app.services.bot_auth import hash_api_key

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app import main as app_main  # noqa: E402


def run_migration_against(scratch_engine):
    """The real startup path (not a copy of it), pointed at a scratch
    engine - main.py's functions read module-level `engine`/`SessionLocal`
    names, so those are what get swapped, exactly like
    test_package_groups.py's own precedent."""
    real_engine, app_main.engine = app_main.engine, scratch_engine
    real_session, app_main.SessionLocal = app_main.SessionLocal, sessionmaker(bind=scratch_engine)
    try:
        app_main._auto_migrate_missing_columns()
        app_main._backfill_api_key_hashes()
    finally:
        app_main.engine = real_engine
        app_main.SessionLocal = real_session


print("=" * 60)
print("--- scenario 1: an OLD database (api_keys exactly as it was before this migration) ---")
print("=" * 60)

old = create_engine("sqlite://", connect_args={"check_same_thread": False})
with old.begin() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE api_keys (
            id INTEGER NOT NULL PRIMARY KEY,
            label VARCHAR(128) NOT NULL,
            key VARCHAR(128) NOT NULL,
            enabled BOOLEAN,
            created_at DATETIME,
            last_used_at DATETIME
        )
    """)
    conn.exec_driver_sql(
        "INSERT INTO api_keys (id, label, key, enabled) VALUES "
        "(1, 'ربات قدیمی', 'old-plaintext-key-one', 1), "
        "(2, 'یکپارچه‌سازی دیگر', 'old-plaintext-key-two', 1)"
    )
check("the old schema really lacks the new columns",
      "key_type" in {c["name"] for c in inspect(old).get_columns("api_keys")}, False)

models.Base.metadata.create_all(old)  # brand-new tables only - api_keys already exists, untouched here
run_migration_against(old)

cols = {c["name"] for c in inspect(old).get_columns("api_keys")}
for expected_col in ("owner_admin_id", "key_type", "capabilities", "scope_enforced",
                     "key_hash", "key_prefix", "key_last4", "created_by_admin_id"):
    check(f"column {expected_col} was added", expected_col in cols, True)

with old.begin() as conn:
    rows = {
        r[0]: r[1:] for r in conn.execute(text(
            "SELECT id, owner_admin_id, key_type, scope_enforced, key_hash, key_prefix, key_last4, label, key "
            "FROM api_keys ORDER BY id"
        )).all()
    }

for key_id, (owner, key_type, scope_enforced, key_hash, key_prefix, key_last4, label, raw) in rows.items():
    check(f"key {key_id}: owner_admin_id backfilled to NULL (legacy)", owner, None)
    check(f"key {key_id}: key_type backfilled to legacy_global", key_type, "legacy_global")
    check(f"key {key_id}: scope_enforced backfilled to False", bool(scope_enforced), False)
    check(f"key {key_id}: key_hash computed from the existing plaintext key",
          key_hash, hash_api_key(raw))
    check(f"key {key_id}: key_prefix/key_last4 derived from the plaintext key",
          (key_prefix, key_last4), (raw[:8], raw[-4:]))
    check(f"key {key_id}: label/plaintext key itself untouched", label is not None and raw is not None, True)

print("\n" + "=" * 60)
print("--- scenario 2: a FRESH database (create_all builds the full current schema directly) ---")
print("=" * 60)

fresh = create_engine("sqlite://", connect_args={"check_same_thread": False})
models.Base.metadata.create_all(fresh)
FreshSession = sessionmaker(bind=fresh)
fdb = FreshSession()
new_key = models.ApiKey(label="یکپارچه‌سازی تازه", key="fresh-plaintext-key")
fdb.add(new_key)
fdb.commit()
fdb.refresh(new_key)

check("a freshly-created key defaults to key_type=legacy_global (Python-side "
      "ORM default, same value Phase A backfills onto old rows)",
      new_key.key_type, "legacy_global")
check("...owner_admin_id defaults to NULL", new_key.owner_admin_id, None)
check("...scope_enforced defaults to False", new_key.scope_enforced, False)
check("...capabilities defaults to NULL ('use key_type default', not 'no capabilities')",
      new_key.capabilities, None)
check("...key_hash is NULL until the backfill runs (not computed at ORM insert time in Phase A)",
      new_key.key_hash, None)

run_migration_against(fresh)
fdb.refresh(new_key)
check("after running the backfill once, the fresh key gets hashed too",
      new_key.key_hash, hash_api_key("fresh-plaintext-key"))
check("...and its prefix/last4", (new_key.key_prefix, new_key.key_last4),
      ("fresh-pl", "fresh-plaintext-key"[-4:]))

print("\n" + "=" * 60)
print("--- scenario 3: startup running again (idempotency) ---")
print("=" * 60)

before_hash = new_key.key_hash
before_prefix = new_key.key_prefix
run_migration_against(fresh)  # second run, same database
fdb.refresh(new_key)
check("running the migration+backfill a SECOND time changes nothing about an "
      "already-migrated key's hash", new_key.key_hash, before_hash)
check("...or its prefix", new_key.key_prefix, before_prefix)
check("...and does not raise (no duplicate-column error, no crash) - confirmed "
      "simply by reaching this line", True, True)

# Also re-run against the OLD (now-migrated) database from scenario 1, to
# prove idempotency holds for an upgraded database too, not just a fresh one.
before = dict(rows)
run_migration_against(old)
with old.begin() as conn:
    after_rows = {
        r[0]: r[1:] for r in conn.execute(text(
            "SELECT id, owner_admin_id, key_type, scope_enforced, key_hash, key_prefix, key_last4, label, key "
            "FROM api_keys ORDER BY id"
        )).all()
    }
check("re-running the full migration against an already-upgraded OLD database "
      "leaves every row byte-for-byte identical",
      after_rows, before)

print("\n" + "=" * 60)
print("--- REGRESSION: a column the backfill does NOT need is missing - it still succeeds ---")
print("=" * 60)
# Reproduces the exact shape of the crash found in review: some OTHER
# column's ALTER failed to apply (created_by_admin_id, here, standing in
# for "any column outside required_columns"), while everything the
# backfill actually touches (id, key, key_hash, key_prefix, key_last4) is
# present. The old, full-ORM-SELECT version would have crashed on this
# too - not because it needed created_by_admin_id, but because loading
# `models.ApiKey` SELECTs every mapped column regardless.

partial = create_engine("sqlite://", connect_args={"check_same_thread": False})
with partial.begin() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE api_keys (
            id INTEGER NOT NULL PRIMARY KEY,
            label VARCHAR(128) NOT NULL,
            key VARCHAR(128) NOT NULL,
            enabled BOOLEAN,
            created_at DATETIME,
            last_used_at DATETIME,
            owner_admin_id INTEGER,
            key_type VARCHAR(32) NOT NULL DEFAULT 'legacy_global',
            capabilities TEXT,
            scope_enforced BOOLEAN NOT NULL DEFAULT 0,
            key_hash VARCHAR(64),
            key_prefix VARCHAR(16),
            key_last4 VARCHAR(8)
            -- created_by_admin_id deliberately absent - simulates its
            -- ALTER having failed while every column the backfill
            -- actually needs succeeded.
        )
    """)
    conn.exec_driver_sql(
        "INSERT INTO api_keys (id, label, key, key_type, scope_enforced) VALUES "
        "(1, 'partial-migration key', 'partial-plaintext-key', 'legacy_global', 0)"
    )

real_engine, app_main.engine = app_main.engine, partial
real_session, app_main.SessionLocal = app_main.SessionLocal, sessionmaker(bind=partial)
try:
    app_main._backfill_api_key_hashes()
finally:
    app_main.engine = real_engine
    app_main.SessionLocal = real_session

with partial.begin() as conn:
    row = conn.execute(text("SELECT key_hash, key_prefix, key_last4 FROM api_keys WHERE id=1")).first()
check("the backfill still runs and hashes the key, even though an UNRELATED "
      "column (created_by_admin_id) is entirely missing from the real table",
      row[0], hash_api_key("partial-plaintext-key"))
check("...and its prefix/last4 too", (row[1], row[2]),
      ("partial-plaintext-key"[:8], "partial-plaintext-key"[-4:]))

print("\n" + "=" * 60)
print("--- REGRESSION: a column the backfill DOES need is missing - clean skip, no crash ---")
print("=" * 60)

broken = create_engine("sqlite://", connect_args={"check_same_thread": False})
with broken.begin() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE api_keys (
            id INTEGER NOT NULL PRIMARY KEY,
            label VARCHAR(128) NOT NULL,
            key VARCHAR(128) NOT NULL,
            enabled BOOLEAN,
            created_at DATETIME,
            last_used_at DATETIME,
            owner_admin_id INTEGER,
            key_type VARCHAR(32) NOT NULL DEFAULT 'legacy_global',
            capabilities TEXT,
            scope_enforced BOOLEAN NOT NULL DEFAULT 0
            -- key_hash/key_prefix/key_last4 all deliberately absent - this
            -- IS one of the columns the backfill needs.
        )
    """)
    conn.exec_driver_sql(
        "INSERT INTO api_keys (id, label, key, key_type, scope_enforced) VALUES "
        "(1, 'broken-migration key', 'broken-plaintext-key', 'legacy_global', 0)"
    )

real_engine, app_main.engine = app_main.engine, broken
real_session, app_main.SessionLocal = app_main.SessionLocal, sessionmaker(bind=broken)
crashed = False
try:
    app_main._backfill_api_key_hashes()
except Exception:
    crashed = True
finally:
    app_main.engine = real_engine
    app_main.SessionLocal = real_session

check("the backfill does NOT raise when key_hash/key_prefix/key_last4 are "
      "missing - it must degrade to 'skip for now', never crash startup",
      crashed, False)
with broken.begin() as conn:
    row2 = conn.execute(text("SELECT label, key FROM api_keys WHERE id=1")).first()
check("the row itself is completely untouched - still there, unchanged",
      (row2[0], row2[1]), ("broken-migration key", "broken-plaintext-key"))

print("\n" + "=" * 60)
print("--- schema-only boundary (superseded by Phase C - see below) ---")
print("=" * 60)

import inspect as pyinspect  # noqa: E402

from app import deps  # noqa: E402
from app.telegram_bot import panel_bridge  # noqa: E402

# This section used to assert that deps/routers/bot.py/panel_bridge.py
# imported NOTHING from bot_auth - Product's explicit Phase A/B limit at
# the time. Phase C's C0 stage (docs/api-key-scope-audit-2026-09-27.md,
# نسخه‌ی هشتم) is exactly the migration THAT wires those in, on purpose -
# so this migration test's own job is now just "deps.get_bot_api_key
# itself (the raw 401 gate) is untouched", not "nothing here ever imports
# bot_auth". The real "still behaves like today" guarantee belongs to
# test_bot_route_policy_coverage.py/test_bot_inprocess_trust_characterization.py,
# not to a bare source-text search here.
check("deps.get_bot_api_key itself (the raw 401 gate) is untouched by this migration - "
      "still returns the plain ApiKey row, no BotPrincipal/bot_auth import in THAT function",
      "bot_auth" in pyinspect.getsource(deps.get_bot_api_key), False)
check("routers/bot.py is now wired to bot_auth (Phase C C0, not this migration)",
      "bot_auth" in pyinspect.getsource(__import__("app.routers.bot", fromlist=["router"])), True)
check("telegram_bot/panel_bridge.py is now wired to bot_auth (Phase C C0, not this migration)",
      "bot_auth" in pyinspect.getsource(panel_bridge), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
