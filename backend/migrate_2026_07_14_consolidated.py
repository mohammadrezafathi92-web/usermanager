"""
usermanager: consolidated migration for the #224-#230 round.
Run inside the backend container (NOT with the sqlite3 CLI, which is not
installed in the image):

    cat backend/migrate_2026_07_14_consolidated.py | docker compose run --rm -T backend python3 -

Safe to re-run - each ALTER is wrapped individually, duplicate-column
errors are caught and skipped, everything else re-raises.
"""
import sqlite3

DB_PATH = "/app/data/usermanager.db"

STATEMENTS = [
    # already-written earlier this round, not yet deployed:
    "ALTER TABLE radius_limit_event_logs ADD COLUMN client_ip VARCHAR(64)",
    "ALTER TABLE users ADD COLUMN reserved_quota_bytes BIGINT",
    "ALTER TABLE users ADD COLUMN reserved_duration_days INTEGER",
    "ALTER TABLE users ADD COLUMN reserved_package_id INTEGER REFERENCES packages(id)",
    "ALTER TABLE users ADD COLUMN reserved_created_at DATETIME",
    # task #229 - per-item customer bot menu toggles:
    "ALTER TABLE bot_settings ADD COLUMN customer_menu_disabled_items TEXT",
]

conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()
for stmt in STATEMENTS:
    try:
        cur.execute(stmt)
        print("OK:  ", stmt)
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("SKIP:", stmt, "->", e)
        else:
            raise
conn.commit()
conn.close()
print("Migration complete.")
