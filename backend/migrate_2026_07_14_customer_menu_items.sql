-- Task #229: per-item enable/disable toggles for the customer bot menu.
-- Adds one new column to the existing bot_settings table. NEW TABLES never
-- need a manual migration (Base.metadata.create_all() creates them on
-- backend startup on its own; only new COLUMNS on EXISTING tables do.

ALTER TABLE bot_settings ADD COLUMN customer_menu_disabled_items TEXT;
