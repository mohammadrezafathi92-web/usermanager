"""One-time backfill for the users.package_id bug: every user created
through the bot's purchase flow (the vast majority of real accounts) ended
up with package_id=NULL forever, because routers/bot.py's create_user/renew
never stamped it (fixed now - see services/user_ops.py). This means the
panel's "filter/select users by package" feature (Users.jsx) shows nothing
for almost everyone until either (a) they get a NEW purchase/renewal under
the fixed code, or (b) this script runs once to retroactively tag existing
users.

Best-effort matching: for every user with package_id IS NULL, look at their
most recent connection's package_name_snapshot (a plain display string
saved on each Connection at purchase time) and match it to a package by
exact name - but ONLY if that name is unique across all packages (skips
ambiguous/duplicate-named packages rather than guessing wrong). Users with
no connections, or whose snapshot doesn't match any current package name
(e.g. the package was since renamed or deleted), are left as NULL - same
as today, just not worse.

Safe to re-run - only ever touches rows that are still NULL.

Run with:
  cat backfill_package_id_2026_07_13.py | docker compose run --rm -T backend python3 -
"""
import sqlite3

conn = sqlite3.connect("/app/data/usermanager.db")
cur = conn.cursor()

pkgs = cur.execute("SELECT id, name FROM packages").fetchall()
name_counts = {}
for _pid, name in pkgs:
    name_counts[name] = name_counts.get(name, 0) + 1
name_to_id = {name: pid for pid, name in pkgs if name_counts[name] == 1}

users = cur.execute("SELECT id FROM users WHERE package_id IS NULL").fetchall()
updated = 0
skipped_no_match = 0
for (uid,) in users:
    row = cur.execute(
        """SELECT package_name_snapshot FROM connections
           WHERE user_id = ? AND package_name_snapshot IS NOT NULL
           ORDER BY id DESC LIMIT 1""",
        (uid,),
    ).fetchone()
    if row and row[0] in name_to_id:
        cur.execute("UPDATE users SET package_id = ? WHERE id = ?", (name_to_id[row[0]], uid))
        updated += 1
    else:
        skipped_no_match += 1

conn.commit()
print(f"backfilled package_id for {updated} of {len(users)} users missing it")
print(f"left NULL (no matching connection/package name): {skipped_no_match}")
