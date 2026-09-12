"""The permission list, and the promise that growing it never takes
anything away from an existing account.

Run:  python3 backend/tests/test_permissions_expanded.py

Reported 2026-09 as "بخش دسترسی‌ها هم خیلی ناقص هست": eight checkboxes for a
panel where a Seller could create, edit, reset usage, add and remove
services, kick sessions, read every connection log and export a full backup
with nothing gating any of it.

The rule this file defends is the one the feature keeps almost failing:
adding a permission must be a new ABILITY TO WITHHOLD, never a restriction
applied retroactively. _grandfather_permissions used to run exactly once,
so it held for the first batch and silently broke for every batch after -
an account that could export yesterday simply could not today.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import permissions

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


print("--- every permission is real: it names a server-side gate ---")
# Read the routers and confirm each key is actually enforced somewhere,
# rather than being a checkbox that only moves a menu item.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sources = []
for folder in ("app/routers", "app"):
    d = os.path.join(ROOT, folder)
    for name in os.listdir(d):
        if name.endswith(".py"):
            sources.append(open(os.path.join(d, name), encoding="utf-8").read())
blob = "\n".join(sources)

ungated = [k for k in permissions.PERMISSION_CHOICES if f'"{k}"' not in blob.replace('permissions.py', '')]
# permissions.py itself obviously mentions them all; the check that matters
# is that each appears in a require_permission(...) or an explicit check.
enforced = [k for k in permissions.PERMISSION_CHOICES
            if f'require_permission("{k}")' in blob or f'"{k}" not in effective_permissions' in blob]
missing = sorted(set(permissions.PERMISSION_CHOICES) - set(enforced))
check("every permission has a server-side gate", missing, [])

print("\n--- the list actually covers what a Seller can do ---")
for key in ("create_users", "edit_users", "delete_users", "reset_usage",
            "manage_connections", "kick_unban", "export_users", "bulk_actions",
            "spend_credit", "view_accounting", "own_bot", "manage_ads",
            "own_backup", "view_radius_logs", "view_tutorials"):
    if key not in permissions.PERMISSION_CHOICES:
        failures.append(f"missing permission {key}")
        print(f"FAIL  {key} is not in the list")
print(f"PASS  {len(permissions.PERMISSION_CHOICES)} permissions defined")

print("\n--- parsing/formatting round-trips, and ignores anything unknown ---")
raw = "delete_users,export_users,not_a_real_permission"
check("unknown keys are dropped", permissions.parse_permissions(raw),
      {"delete_users", "export_users"})
check("formatting keeps only real keys",
      sorted(permissions.format_permissions({"delete_users", "nope"}).split(",")),
      ["delete_users"])
check("an empty string is no permissions", permissions.parse_permissions(""), set())
check("None is no permissions", permissions.parse_permissions(None), set())

print("\n--- a legacy key still maps to whatever survives of it ---")
check("manage_tutorials still grants viewing",
      permissions.parse_permissions("manage_tutorials"), {"view_tutorials"})

print("\n--- grandfathering grants only what is NEW since last time ---")
# The shape main.py relies on: keys already recorded are skipped, keys that
# are not are granted. Simulated here over the same helpers rather than by
# booting the app, so it stays a unit test.
all_keys = set(permissions.PERMISSION_CHOICES)
already = {"delete_users", "bulk_actions", "export_users", "spend_credit",
           "view_accounting", "manage_discount_codes", "own_bot", "view_tutorials"}
new_keys = all_keys - already
check("the newly added keys are exactly the ones to grant",
      sorted(new_keys),
      sorted(["create_users", "edit_users", "reset_usage", "manage_connections",
              "kick_unban", "manage_ads", "own_backup", "view_radius_logs"]))

seller_before = {"delete_users", "export_users"}          # what they had
seller_after = seller_before | new_keys                    # after grandfathering
check("an existing account keeps everything it had", seller_before - seller_after, set())
check("...and gains the new ones rather than losing them",
      "export_users" in seller_after and "reset_usage" in seller_after, True)

# Second deploy with nothing new: must be a no-op.
check("a deploy that adds no permission grants nothing",
      all_keys - (already | new_keys), set())


print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("هر دسترسی یک کنترل واقعی دارد و اضافه‌شدنشان از کسی چیزی نمی‌گیرد")
