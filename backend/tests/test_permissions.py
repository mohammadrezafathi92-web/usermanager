"""The granular Seller permissions, and the promise that nobody loses an
ability on the deploy that introduces them.

Run:  python3 backend/tests/test_permissions.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, permissions
from app.services import hierarchy

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


NEW_KEYS = {
    "delete_users", "bulk_actions", "export_users", "spend_credit",
    "view_accounting", "manage_discount_codes", "own_bot", "view_tutorials",
}

print("--- the set itself ---")
check("every agreed capability is grantable", set(permissions.PERMISSION_CHOICES), NEW_KEYS)
check("every key belongs to exactly one group",
      sum(len(g["perms"]) for g in permissions.PERMISSION_GROUPS.values()),
      len(permissions.PERMISSION_CHOICES))
check("every key has a human label",
      all(v.strip() for v in permissions.PERMISSION_CHOICES.values()), True)
# Creating/editing customers must stay ungated - a Seller who cannot do
# that has no reason to exist.
check("creating a customer is not a permission", "create_users" in permissions.PERMISSION_CHOICES, False)
check("editing a customer is not a permission", "edit_users" in permissions.PERMISSION_CHOICES, False)

print("\n--- who the gates apply to ---")
from app.deps import require_permission


def allowed(admin, perm) -> bool:
    try:
        require_permission(perm)(admin=admin)
        return True
    except HTTPException:
        return False


class FakeAdmin:
    def __init__(self, *, superadmin=False, role="seller", perms=""):
        self.is_superadmin = superadmin
        self.role = role
        self.parent_admin_id = 1 if role == "seller" else None
        self.permissions = perms
        self.group = None


su = FakeAdmin(superadmin=True, role="superadmin")
lvl2 = FakeAdmin(role="admin")
seller_none = FakeAdmin(role="seller", perms="")
seller_some = FakeAdmin(role="seller", perms="delete_users,view_tutorials")

for perm in sorted(NEW_KEYS):
    if not allowed(su, perm):
        failures.append(f"superadmin blocked from {perm}")
    if not allowed(lvl2, perm):
        failures.append(f"level-2 admin blocked from {perm}")
print(f"PASS  a superadmin passes all {len(NEW_KEYS)} gates" if not failures else "FAIL  superadmin gates")
print("PASS  a level-2 Admin passes all gates (they are never restricted)")

check("a seller with nothing is refused", allowed(seller_none, "delete_users"), False)
check("a seller is allowed what they hold", allowed(seller_some, "delete_users"), True)
check("...and refused what they do not", allowed(seller_some, "bulk_actions"), False)

print("\n--- a group overrides the account's own list ---")
class FakeGroup:
    permissions = "export_users"


grouped = FakeAdmin(role="seller", perms="delete_users,bulk_actions")
grouped.group = FakeGroup()
check("the group's list is what counts",
      permissions.effective_permissions(grouped), {"export_users"})
check("the account's own list is ignored while grouped",
      allowed(grouped, "delete_users"), False)
check("the group's permission applies", allowed(grouped, "export_users"), True)

print("\n--- storage round-trips and rejects junk ---")
check("unknown keys are dropped on read",
      permissions.parse_permissions("delete_users,fly_to_the_moon"), {"delete_users"})
check("unknown keys are dropped on write",
      permissions.format_permissions({"delete_users", "nonsense"}), "delete_users")
check("empty is empty", permissions.parse_permissions(""), set())
check("None is empty", permissions.parse_permissions(None), set())
check("whitespace is tolerated",
      permissions.parse_permissions(" delete_users , own_bot "), {"delete_users", "own_bot"})
# The old broad toggles still map to what survives of them.
check("a legacy toggle still resolves",
      permissions.parse_permissions("manage_tutorials"), {"view_tutorials"})

print("\n--- nobody loses an ability on the upgrade ---")
from app import main as app_main


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


Session = make_db()
db = Session()
db.add(models.PanelSettings(id=1))
group = models.AdminPermissionGroup(name="فروشنده استاندارد", permissions="view_tutorials")
db.add(group)
db.commit()
su_row = models.AdminUser(username="su", hashed_password="x", is_superadmin=True, permissions="")
a_row = models.AdminUser(username="a", hashed_password="x", role="admin", permissions="")
s_row = models.AdminUser(username="s", hashed_password="x", role="seller", permissions="view_tutorials")
db.add_all([su_row, a_row, s_row])
db.commit()
db.close()

app_main.SessionLocal = Session
app_main._grandfather_permissions()

db = Session()
s_after = db.query(models.AdminUser).filter_by(username="s").one()
check("an existing Seller keeps everything they could do",
      permissions.parse_permissions(s_after.permissions), NEW_KEYS)
g_after = db.query(models.AdminPermissionGroup).one()
check("so does an existing group", permissions.parse_permissions(g_after.permissions), NEW_KEYS)
su_after = db.query(models.AdminUser).filter_by(username="su").one()
check("the superadmin's own list is left alone", su_after.permissions, "")
check("the run is marked done",
      db.query(models.PanelSettings).first().permissions_grandfathered, True)

# Second run must change nothing - the superadmin's later choices must not
# be undone by a restart.
s_after.permissions = "view_tutorials"
db.commit()
db.close()
app_main._grandfather_permissions()
db = Session()
check("a restart does not re-grant what was deliberately removed",
      db.query(models.AdminUser).filter_by(username="s").one().permissions, "view_tutorials")
db.close()

print("\n--- a fresh install is not grandfathered ---")
Session2 = make_db()
db = Session2()
db.add(models.PanelSettings(id=1, permissions_grandfathered=False))
db.commit()
db.close()
app_main.SessionLocal = Session2
app_main._grandfather_permissions()
db = Session2()
check("no accounts, nothing to do, still marked done",
      db.query(models.PanelSettings).first().permissions_grandfathered, True)
db.close()

print("\n--- default preset groups must not silently withhold new permissions ---")
# Reported 2026-08-28: a Seller in "فروشنده استاندارد" could not enable
# their own bot - own_bot (and delete_users/bulk_actions/export_users/
# spend_credit/view_accounting/manage_discount_codes) was never in these
# two groups' permission strings, because they were seeded back when
# view_tutorials was the only real permission left, and grandfathering
# only ever runs once - long before these groups exist on a fresh install.
Session3 = make_db()
db = Session3()
db.commit()
db.close()
app_main.SessionLocal = Session3
app_main._seed_default_permission_groups()

db = Session3()
standard = db.query(models.AdminPermissionGroup).filter_by(name="فروشنده استاندارد").one()
limited = db.query(models.AdminPermissionGroup).filter_by(name="فروشنده محدود (بدون آموزش)").one()
check("a fresh install's standard-seller preset gets EVERY current permission",
      permissions.parse_permissions(standard.permissions), NEW_KEYS)
check("...including own_bot specifically (the reported gap)",
      "own_bot" in permissions.parse_permissions(standard.permissions), True)
check("the limited preset gets everything EXCEPT view_tutorials, matching its name",
      permissions.parse_permissions(limited.permissions), NEW_KEYS - {"view_tutorials"})
check("...it still has own_bot too - only tutorials was ever supposed to be withheld",
      "own_bot" in permissions.parse_permissions(limited.permissions), True)
db.close()

print("\n--- an install that already seeded the OLD narrow values gets repaired ---")
Session4 = make_db()
db = Session4()
# Exactly what _seed_default_permission_groups used to write, before this
# fix - the real state of an existing production install.
db.add(models.AdminPermissionGroup(name="فروشنده استاندارد", permissions="view_tutorials"))
db.add(models.AdminPermissionGroup(name="فروشنده محدود (بدون آموزش)", permissions=""))
# A superadmin's own custom group must never be touched by this repair.
db.add(models.AdminPermissionGroup(name="گروه دلخواه من", permissions="own_bot"))
db.commit()
db.close()
app_main.SessionLocal = Session4
app_main._repair_stale_default_permission_groups()

db = Session4()
standard2 = db.query(models.AdminPermissionGroup).filter_by(name="فروشنده استاندارد").one()
limited2 = db.query(models.AdminPermissionGroup).filter_by(name="فروشنده محدود (بدون آموزش)").one()
custom = db.query(models.AdminPermissionGroup).filter_by(name="گروه دلخواه من").one()
check("the stale standard-seller preset was upgraded to everything",
      permissions.parse_permissions(standard2.permissions), NEW_KEYS)
check("the stale limited preset was upgraded to everything except tutorials",
      permissions.parse_permissions(limited2.permissions), NEW_KEYS - {"view_tutorials"})
check("a superadmin's own custom group was left completely untouched",
      custom.permissions, "own_bot")
db.close()

print("\n--- the repair is safe to run again, and never touches a later edit ---")
app_main._repair_stale_default_permission_groups()
db = Session4()
standard3 = db.query(models.AdminPermissionGroup).filter_by(name="فروشنده استاندارد").one()
check("a second run changes nothing further", standard3.permissions, standard2.permissions)

# Simulate the superadmin deliberately narrowing the standard preset AFTER
# it was fixed - a later restart must not silently widen it back out.
standard3.permissions = "delete_users"
db.commit()
db.close()
app_main._repair_stale_default_permission_groups()
db = Session4()
check("a superadmin's later, deliberate edit is never overwritten by this repair",
      db.query(models.AdminPermissionGroup).filter_by(name="فروشنده استاندارد").one().permissions,
      "delete_users")
db.close()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
