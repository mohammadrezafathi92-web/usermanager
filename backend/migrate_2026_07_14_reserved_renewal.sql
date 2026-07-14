-- usermanager: adds the "reserved renewal" (بسته رزرو) columns to users -
-- when a renewal is requested while the current quota/expiry both still
-- have room left, it's queued here instead of applied immediately, and
-- auto-activates once the current package actually runs out (see
-- services/user_ops.py's renew_user / _maybe_activate_reserved_renewal).
-- Safe to re-run - a second run just fails each line with "duplicate
-- column name", which is fine to ignore.

ALTER TABLE users ADD COLUMN reserved_quota_bytes BIGINT;
ALTER TABLE users ADD COLUMN reserved_duration_days INTEGER;
ALTER TABLE users ADD COLUMN reserved_package_id INTEGER REFERENCES packages(id);
ALTER TABLE users ADD COLUMN reserved_created_at DATETIME;
