-- usermanager: combined migration for this round of features (packages
-- bot/web visibility split, package-scoped bulk actions, panel web port).
-- Run this ONCE against the production sqlite database after deploying the
-- new code, e.g.:
--   docker exec -it usermanager-backend sh -lc "sqlite3 /app/data/usermanager.db" < migrate_2026_07_13.sql
-- or copy this file into the container and run sqlite3 against it directly.
-- Safe to re-run: if a column already exists, that one line will fail with
-- "duplicate column name" - just remove that line and re-run the rest.

-- Task #199: package-scoped bulk actions in Users.jsx
ALTER TABLE users ADD COLUMN package_id INTEGER;
CREATE INDEX IF NOT EXISTS ix_users_package_id ON users(package_id);

-- Task #200: split package visibility into web-panel vs bot toggles
ALTER TABLE packages ADD COLUMN bot_enabled BOOLEAN DEFAULT 1;

-- Task #204: configurable panel web port via Settings (SSH-based)
ALTER TABLE panel_settings ADD COLUMN panel_web_port INTEGER DEFAULT 80;
ALTER TABLE panel_settings ADD COLUMN panel_ssh_host VARCHAR(255);
ALTER TABLE panel_settings ADD COLUMN panel_ssh_port INTEGER DEFAULT 22;
ALTER TABLE panel_settings ADD COLUMN panel_ssh_username VARCHAR(100) DEFAULT 'root';
ALTER TABLE panel_settings ADD COLUMN panel_project_dir VARCHAR(255) DEFAULT '/root/usermanager';
ALTER TABLE panel_settings ADD COLUMN panel_port_status TEXT;
ALTER TABLE panel_settings ADD COLUMN panel_port_changed_at DATETIME;
