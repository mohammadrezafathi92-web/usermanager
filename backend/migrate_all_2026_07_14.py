"""usermanager: comprehensive catch-up migration.

srv1 turned out to be missing several rounds of ALTER TABLE migrations that
were already applied elsewhere (confirmed by the startup crash: "no such
column: panel_settings.support_contact_text" - from the 2026-07-13
referral/loyalty/discount/support round). Rather than trying to figure out
exactly which of the several migrate_2026_07_*.sql files were or weren't
run on this particular server, this script bundles EVERY ALTER TABLE /
CREATE INDEX statement from all of them into one idempotent run - each
statement is wrapped individually, "duplicate column name" (already
applied) and "index already exists" are silently skipped, anything else
re-raises. Safe to run on any server regardless of which prior migrations
it already has.

Run inside the backend container (NOT with the sqlite3 CLI, which isn't
installed in the image):

    docker compose -f /opt/usermanager/docker-compose.yml exec -T backend python3 - < backend/migrate_all_2026_07_14.py

or, from the project root on the server:

    docker compose exec -T backend python3 - < migrate_all_2026_07_14.py
"""
import sqlite3

DB_PATH = "/app/data/usermanager.db"

STATEMENTS = [
    # ---- 2026-07-13: package-scoped bulk actions, bot/web package
    # visibility split, panel web port ----
    "ALTER TABLE users ADD COLUMN package_id INTEGER",
    "CREATE INDEX IF NOT EXISTS ix_users_package_id ON users(package_id)",
    "ALTER TABLE packages ADD COLUMN bot_enabled BOOLEAN DEFAULT 1",
    "ALTER TABLE panel_settings ADD COLUMN panel_web_port INTEGER DEFAULT 80",
    "ALTER TABLE panel_settings ADD COLUMN panel_ssh_host VARCHAR(255)",
    "ALTER TABLE panel_settings ADD COLUMN panel_ssh_port INTEGER DEFAULT 22",
    "ALTER TABLE panel_settings ADD COLUMN panel_ssh_username VARCHAR(100) DEFAULT 'root'",
    "ALTER TABLE panel_settings ADD COLUMN panel_project_dir VARCHAR(255) DEFAULT '/root/usermanager'",
    "ALTER TABLE panel_settings ADD COLUMN panel_port_status TEXT",
    "ALTER TABLE panel_settings ADD COLUMN panel_port_changed_at DATETIME",

    # ---- 2026-07-13: support text, referral program, loyalty rewards ----
    "ALTER TABLE panel_settings ADD COLUMN support_contact_text TEXT",
    "ALTER TABLE panel_settings ADD COLUMN referral_referrer_reward_credit INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE panel_settings ADD COLUMN referral_referrer_reward_gb REAL NOT NULL DEFAULT 0",
    "ALTER TABLE panel_settings ADD COLUMN referral_new_user_reward_credit INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE panel_settings ADD COLUMN referral_new_user_reward_gb REAL NOT NULL DEFAULT 0",
    "ALTER TABLE panel_settings ADD COLUMN loyalty_purchase_threshold INTEGER",
    "ALTER TABLE panel_settings ADD COLUMN loyalty_reward_credit INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE panel_settings ADD COLUMN loyalty_reward_gb REAL NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN referral_code VARCHAR(16)",
    "ALTER TABLE users ADD COLUMN referred_by_id INTEGER REFERENCES users(id)",
    "ALTER TABLE users ADD COLUMN referral_reward_granted BOOLEAN DEFAULT 0",
    "ALTER TABLE users ADD COLUMN purchase_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE users ADD COLUMN loyalty_rewards_given INTEGER NOT NULL DEFAULT 0",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code ON users(referral_code)",
    "CREATE INDEX IF NOT EXISTS ix_users_referred_by_id ON users(referred_by_id)",

    # ---- 2026-07-14: RADIUS limit-log client IP, reserved renewal,
    # customer bot menu per-item toggles ----
    "ALTER TABLE radius_limit_event_logs ADD COLUMN client_ip VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN reserved_quota_bytes BIGINT",
    "ALTER TABLE users ADD COLUMN reserved_duration_days INTEGER",
    "ALTER TABLE users ADD COLUMN reserved_package_id INTEGER REFERENCES packages(id)",
    "ALTER TABLE users ADD COLUMN reserved_created_at DATETIME",
    "ALTER TABLE bot_settings ADD COLUMN customer_menu_disabled_items TEXT",
]

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
for stmt in STATEMENTS:
    try:
        cur.execute(stmt)
        print("OK:  ", stmt)
    except sqlite3.OperationalError as e:
        msg = str(e)
        if "duplicate column name" in msg or "already exists" in msg:
            print("SKIP:", stmt, "->", e)
        else:
            raise
conn.commit()
conn.close()
print("Migration complete.")
