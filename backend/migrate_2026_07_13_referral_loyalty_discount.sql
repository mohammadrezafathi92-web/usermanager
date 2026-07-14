-- usermanager: migration for the support/referral/loyalty/discount-code
-- round (support contact text, invite-a-friend referral program, automatic
-- loyalty rewards after N purchases, and admin-managed discount codes).
--
-- Run this ONCE against the production sqlite database after deploying the
-- new code, e.g.:
--   docker exec -it usermanager-backend sh -lc "sqlite3 /app/data/usermanager.db" < migrate_2026_07_13_referral_loyalty_discount.sql
-- or copy this file into the container and run sqlite3 against it directly.
--
-- Safe to re-run: if a column already exists, that one line will fail with
-- "duplicate column name" - just remove that line and re-run the rest.
--
-- NOTE: the new discount_codes / discount_code_redemptions tables need NO
-- migration - SQLAlchemy's Base.metadata.create_all() (run automatically on
-- backend startup) creates any missing TABLE on its own; only new COLUMNS
-- on EXISTING tables (panel_settings, users, below) need this manual ALTER.

-- PanelSettings: support contact text + referral reward amounts + loyalty
-- program settings (all default to "feature disabled" - 0/NULL).
ALTER TABLE panel_settings ADD COLUMN support_contact_text TEXT;
ALTER TABLE panel_settings ADD COLUMN referral_referrer_reward_credit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE panel_settings ADD COLUMN referral_referrer_reward_gb REAL NOT NULL DEFAULT 0;
ALTER TABLE panel_settings ADD COLUMN referral_new_user_reward_credit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE panel_settings ADD COLUMN referral_new_user_reward_gb REAL NOT NULL DEFAULT 0;
ALTER TABLE panel_settings ADD COLUMN loyalty_purchase_threshold INTEGER;
ALTER TABLE panel_settings ADD COLUMN loyalty_reward_credit INTEGER NOT NULL DEFAULT 0;
ALTER TABLE panel_settings ADD COLUMN loyalty_reward_gb REAL NOT NULL DEFAULT 0;

-- Users: each user's own invite code + who referred them + reward/loyalty
-- bookkeeping. referral_code stays NULL for every EXISTING user until the
-- one-time backfill step below runs (brand-new users get one automatically
-- at creation - see services/user_ops.py's _generate_referral_code).
ALTER TABLE users ADD COLUMN referral_code VARCHAR(16);
ALTER TABLE users ADD COLUMN referred_by_id INTEGER REFERENCES users(id);
ALTER TABLE users ADD COLUMN referral_reward_granted BOOLEAN DEFAULT 0;
ALTER TABLE users ADD COLUMN purchase_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN loyalty_rewards_given INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_referral_code ON users(referral_code);
CREATE INDEX IF NOT EXISTS ix_users_referred_by_id ON users(referred_by_id);
