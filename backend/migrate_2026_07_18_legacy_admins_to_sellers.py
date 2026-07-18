"""
usermanager: one-off data fix for the 3-tier hierarchy feature.

Context: before the hierarchy feature existed, every non-superadmin account
had parent_admin_id = NULL. hierarchy.role() treats "non-superadmin, no
parent" as a level-2 Admin (full-tree bypass access) - which is correct for
accounts genuinely meant to be independent resellers, but WRONG for the
user's actual pre-existing accounts, which were always meant to be Sellers
working directly under the superadmin (see chat: "admin hai ghabli ke man
sakhtam froshande bode" - the admins I created before were actually
sellers).

This script reparents every such legacy account to the superadmin
(parent_admin_id = superadmin.id), turning them into "Seller under
superadmin" - permission-gated instead of full-bypass, and no longer
eligible for their own node-assignment/bot/backup features (those stay
level-2-Admin-only, see services/hierarchy.py).

Safety:
- Only touches accounts with is_superadmin=0 AND parent_admin_id IS NULL.
- SKIPS any such account that already has its OWN sub-accounts (i.e. is
  actively being used as a level-2 hub with real Sellers under it) -
  demoting those would require cascading their children too, which is a
  judgment call better made manually via the "تغییر نقش" control in the
  Admins page. These are printed at the end for manual review.
- For a converted account with NO permission group and an EMPTY
  `permissions` column (i.e. it never needed granular permissions before,
  because it had full bypass access), grants a sensible default permission
  set so it doesn't suddenly lose access to pages it was actively using -
  packages, tutorials, payment settings, API keys, discount codes. Node
  management is deliberately NOT included (Sellers never get node access
  regardless - see hierarchy.py's accessible_node_ids). Bot-settings/backup
  permissions are also deliberately NOT included since those are already
  superadmin-only / level-2-Admin-only regardless of this checkbox.
- Idempotent: safe to re-run - accounts already reparented (parent_admin_id
  set) are simply not matched by the WHERE clause a second time.

Run inside the backend container:

    cat backend/migrate_2026_07_18_legacy_admins_to_sellers.py | docker compose run --rm -T backend python3 -
"""
import sqlite3

DB_PATH = "/app/data/usermanager.db"

DEFAULT_SELLER_PERMISSIONS = ",".join([
    "view_packages", "edit_packages", "delete_packages",
    "view_tutorials", "edit_tutorials", "delete_tutorials",
    "manage_payment_settings", "manage_api_keys", "manage_discount_codes",
])

conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

cur.execute("SELECT id, username FROM admin_users WHERE is_superadmin = 1 ORDER BY id LIMIT 1")
superadmin = cur.fetchone()
if not superadmin:
    print("No superadmin found - aborting, nothing changed.")
    conn.close()
    raise SystemExit(1)

superadmin_id = superadmin["id"]
print(f"Superadmin: id={superadmin_id} username={superadmin['username']}")

cur.execute(
    "SELECT id, username, group_id, permissions FROM admin_users "
    "WHERE is_superadmin = 0 AND parent_admin_id IS NULL"
)
candidates = cur.fetchall()

cur.execute("SELECT DISTINCT parent_admin_id FROM admin_users WHERE parent_admin_id IS NOT NULL")
has_children_ids = {row["parent_admin_id"] for row in cur.fetchall()}

converted = []
skipped_has_children = []

for row in candidates:
    if row["id"] in has_children_ids:
        skipped_has_children.append(row["username"])
        continue

    if row["group_id"] is None and not (row["permissions"] or "").strip():
        cur.execute(
            "UPDATE admin_users SET parent_admin_id = ?, permissions = ? WHERE id = ?",
            (superadmin_id, DEFAULT_SELLER_PERMISSIONS, row["id"]),
        )
    else:
        # Already has a group or explicit permissions configured - leave
        # those exactly as-is, just reparent.
        cur.execute(
            "UPDATE admin_users SET parent_admin_id = ? WHERE id = ?",
            (superadmin_id, row["id"]),
        )
    converted.append(row["username"])

conn.commit()
conn.close()

print(f"Converted to Seller under superadmin ({len(converted)}): {converted}")
if skipped_has_children:
    print(
        f"SKIPPED - these already have their own sub-accounts, review manually "
        f"via Admins > تغییر نقش ({len(skipped_has_children)}): {skipped_has_children}"
    )
print("Done.")
