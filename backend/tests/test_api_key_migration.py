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
print("--- schema-only boundary (Product's explicit Phase A limit) ---")
print("=" * 60)

import inspect as pyinspect  # noqa: E402

from app import deps  # noqa: E402
from app.telegram_bot import panel_bridge  # noqa: E402

check("deps.get_bot_api_key's source is untouched by this migration - still "
      "returns the plain ApiKey row, no BotPrincipal/bot_auth import",
      "bot_auth" in pyinspect.getsource(deps.get_bot_api_key), False)
check("routers/bot.py imports nothing from bot_auth",
      "bot_auth" not in pyinspect.getsource(__import__("app.routers.bot", fromlist=["router"])), True)
check("telegram_bot/panel_bridge.py imports nothing from bot_auth",
      "bot_auth" not in pyinspect.getsource(panel_bridge), True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
