-- usermanager: adds client_ip to radius_limit_event_logs (the "IP" column
-- now shown on the RADIUS limit-log page and the user-detail mini table).
-- Safe to re-run - a second run just fails with "duplicate column name".

ALTER TABLE radius_limit_event_logs ADD COLUMN client_ip VARCHAR(64);
