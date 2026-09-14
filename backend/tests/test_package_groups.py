"""Shelves in the Mini App's shop (models.PackageGroup).

Run:  python3 backend/tests/test_package_groups.py

A group is a name and a switch, so almost nothing here is about groups. It
is about the three ways a feature like this quietly does damage on a panel
that several resellers share:

  1. Reaching into another reseller's tree. group_id is the only field on a
     package that names another row BY ID and arrives in the request body,
     so without a check an Admin could file their package under a rival's
     group and have it appear inside the rival's shop.
  2. Taking plans off sale by accident. Every package that exists on the
     day this ships is ungrouped and has no miniapp_enabled column yet -
     both have to keep selling, untouched.
  3. Deleting more than was asked for. Removing a shelf must empty it, not
     destroy what was on it.
"""
from __future__ import annotations

import calendar
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import miniapp, package_groups, packages as packages_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except HTTPException as exc:
        return f"{exc.status_code}: {exc.detail}"


ALI_TOKEN = "8000000002:AAali"
REZA_TOKEN = "8000000003:AAreza"


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(models.AdminUser(id=1, username="root", hashed_password="x", is_superadmin=True))
    db.add(models.AdminUser(id=2, username="ali", hashed_password="x",
                            own_bot_token=ALI_TOKEN, telegram_id=111, balance=5_000_000))
    db.add(models.AdminUser(id=3, username="reza", hashed_password="x",
                            own_bot_token=REZA_TOKEN, telegram_id=222))
    db.commit()
    return db, engine


def admin(db, admin_id):
    return db.get(models.AdminUser, admin_id)


def add_package(db, name, owner, group_id=None, miniapp_enabled=True, node=True):
    pkg = models.Package(name=name, quota_gb=10, duration_days=30, price=1000,
                         enabled=True, bot_enabled=True, miniapp_enabled=miniapp_enabled,
                         owner_admin_id=owner, group_id=group_id)
    db.add(pkg)
    db.commit()
    if node:
        # A bundled package - the Mini App refuses to sell one without a
        # services list, so an unbundled one would drop out for the wrong
        # reason and make these assertions lie.
        if not db.get(models.Node, 1):
            db.add(models.Node(id=1, name="n", type=models.NodeType.mikrotik,
                               mt_host="1.2.3.4", mt_username="u", mt_password="p"))
            db.commit()
        db.add(models.PackageConnection(package_id=pkg.id, node_id=1,
                                        protocol=models.ConnectionType.wireguard))
        db.commit()
    return pkg


def sign(bot_token, telegram_id=555):
    fields = {
        "user": json.dumps({"id": telegram_id, "first_name": "Cust"}),
        "auth_date": str(calendar.timegm(dt.datetime.utcnow().utctimetuple())),
    }
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def visitor(db, token=ALI_TOKEN):
    return miniapp.current_visitor(x_telegram_init_data=sign(token), db=db)


# --------------------------------------------------------------------------
print("--- a group belongs to whoever made it ---")
db, engine = make_db()

ali_group = package_groups.create_group(
    schemas.PackageGroupCreate(name="خانوادگی"), db=db, admin=admin(db, 2)
)
reza_group = package_groups.create_group(
    schemas.PackageGroupCreate(name="مال رضا"), db=db, admin=admin(db, 3)
)
check("ownership comes from who is asking", ali_group.owner_admin_id, 2)
check("...not from the payload", reza_group.owner_admin_id, 3)

ali_sees = [g.name for g in package_groups.list_groups(db=db, admin=admin(db, 2))]
check("Ali sees his own", ali_sees, ["خانوادگی"])
check("...and not Reza's", "مال رضا" in ali_sees, False)

# A superadmin is NOT a super-user here: they see their own (NULL-owned)
# scope only, same as every other per-tenant resource in this panel.
root_group = package_groups.create_group(
    schemas.PackageGroupCreate(name="سراسری"), db=db, admin=admin(db, 1)
)
check("a superadmin's group is global (NULL-owned)", root_group.owner_admin_id, None)
check("a superadmin does not see a reseller's groups",
      [g.name for g in package_groups.list_groups(db=db, admin=admin(db, 1))], ["سراسری"])

print("\n--- and cannot be reached by id from outside ---")
check("Ali cannot read Reza's group",
      str(call(package_groups.update_group, reza_group.id,
               schemas.PackageGroupUpdate(name="hijacked"), db=db, admin=admin(db, 2))).startswith("404"),
      True)
check("...nor delete it",
      str(call(package_groups.delete_group, reza_group.id, db=db, admin=admin(db, 2))).startswith("404"),
      True)
check("Reza's group is untouched", db.get(models.PackageGroup, reza_group.id).name, "مال رضا")


# --------------------------------------------------------------------------
print("\n--- a package cannot be filed on someone else's shelf ---")
# The hole this closes: group_id is the one package field that names another
# row by id and comes straight from the request body.
refusal = call(
    packages_router.create_package,
    schemas.PackageCreate(name="نفوذی", quota_gb=1, price=1, group_id=reza_group.id),
    db=db, admin=admin(db, 2),
)
check("refused at creation", str(refusal).startswith("404"), True)
check("...as 'not found', so ids cannot be probed", "پیدا نشد" in str(refusal), True)

mine = add_package(db, "پلن علی", owner=2)
refusal = call(
    packages_router.update_package, mine.id,
    schemas.PackageUpdate(group_id=reza_group.id), db=db, admin=admin(db, 2),
)
check("refused on update too", str(refusal).startswith("404"), True)
db.refresh(mine)
check("the package kept no group", mine.group_id, None)
check("his own group is accepted",
      packages_router.update_package(mine.id, schemas.PackageUpdate(group_id=ali_group.id),
                                     db=db, admin=admin(db, 2)).group_id,
      ali_group.id)


# --------------------------------------------------------------------------
print("\n--- deleting a shelf empties it, it does not destroy it ---")
db, engine = make_db()
group = package_groups.create_group(schemas.PackageGroupCreate(name="فصلی"), db=db, admin=admin(db, 2))
a = add_package(db, "الف", owner=2, group_id=group.id)
b = add_package(db, "ب", owner=2, group_id=group.id)

result = package_groups.delete_group(group.id, db=db, admin=admin(db, 2))
check("it reports how many it let go", result["ungrouped_packages"], 2)
check("the group is gone", db.get(models.PackageGroup, group.id), None)
check("both packages still exist", db.query(models.Package).count(), 2)
db.refresh(a)
db.refresh(b)
check("...and are simply ungrouped", (a.group_id, b.group_id), (None, None))
check("...and still on sale", (a.enabled, a.miniapp_enabled), (True, True))


# --------------------------------------------------------------------------
print("\n--- what the shop is made of ---")
db, engine = make_db()
fam = package_groups.create_group(
    schemas.PackageGroupCreate(name="خانوادگی", description="چند دستگاه", sort_order=1),
    db=db, admin=admin(db, 2),
)
solo = package_groups.create_group(
    schemas.PackageGroupCreate(name="تک‌کاربره", sort_order=0), db=db, admin=admin(db, 2)
)
add_package(db, "تک ۱", owner=2, group_id=solo.id)
add_package(db, "خانواده ۱", owner=2, group_id=fam.id)
add_package(db, "بی‌دسته", owner=2)

shelves = miniapp._shop_shelves(db, 2)
check("one card per group, in the admin's own order",
      [s["name"] for s in shelves], ["تک‌کاربره", "خانوادگی", "سایر پلن‌ها"])
check("the group's description rides along", shelves[1]["description"], "چند دستگاه")
check("each card holds its own plans",
      [[p.name for p in s["packages"]] for s in shelves],
      [["تک ۱"], ["خانواده ۱"], ["بی‌دسته"]])

print("\n--- the switches ---")
# miniapp_enabled off: gone from the shop, still in the bot.
hidden = add_package(db, "فقط در ربات", owner=2, group_id=solo.id, miniapp_enabled=False)
shelves = miniapp._shop_shelves(db, 2)
check("a plan hidden from the Mini App is not on any shelf",
      any(p.name == "فقط در ربات" for s in shelves for p in s["packages"]), False)
check("...but the bot still sells it",
      any(p.name == "فقط در ربات" for p in
          __import__("app.routers.bot", fromlist=["x"]).list_packages(owner_admin_id=2, db=db)),
      True)
check("...and it cannot be bought by id either",
      str(call(miniapp._package_or_404, db, 2, hidden.id)).startswith("404"), True)

# A shelf switched off: the card goes, its plans do not.
package_groups.update_group(fam.id, schemas.PackageGroupUpdate(enabled=False), db=db, admin=admin(db, 2))
shelves = miniapp._shop_shelves(db, 2)
check("the disabled shelf's card is gone", [s["name"] for s in shelves], ["تک‌کاربره", "سایر پلن‌ها"])
check("...but its plan is still on sale, under «سایر پلن‌ها»",
      [p.name for p in shelves[-1]["packages"]], ["خانواده ۱", "بی‌دسته"])

print("\n--- shelves are scoped by the signature, like everything else ---")
add_package(db, "پلن رضا", owner=3)
check("Ali's shop shows nothing of Reza's",
      any(p.name == "پلن رضا" for s in miniapp._shop_shelves(db, 2) for p in s["packages"]), False)
home = miniapp.home(visitor=visitor(db, ALI_TOKEN), db=db)
check("...and /home is built from the same shelves",
      [s["name"] for s in home["shop"]["groups"]], ["تک‌کاربره", "سایر پلن‌ها"])

print("\n--- an empty shelf is not drawn ---")
package_groups.create_group(schemas.PackageGroupCreate(name="خالی", sort_order=9), db=db, admin=admin(db, 2))
check("a group with no packages produces no card",
      any(s["name"] == "خالی" for s in miniapp._shop_shelves(db, 2)), False)

print("\n--- a shop with no groups at all ---")
db, engine = make_db()
add_package(db, "تنها پلن", owner=2)
shelves = miniapp._shop_shelves(db, 2)
check("one unnamed card, not a card called «سایر پلن‌ها»",
      [(s["name"], [p.name for p in s["packages"]]) for s in shelves],
      [("", ["تنها پلن"])])


# --------------------------------------------------------------------------
print("\n--- upgrading a panel that already has packages ---")
# The real risk on deploy day: every existing package is ungrouped and its
# row has no miniapp_enabled column yet. Neither may take it off sale.
# An "old" panel: the packages table exactly as it was before this feature,
# written out by hand rather than by dropping columns off the current one -
# SQLite refuses to drop a column a foreign key still mentions, and the
# point here is to start from a schema that never knew about groups at all.
old = create_engine("sqlite://", connect_args={"check_same_thread": False})
with old.begin() as conn:
    conn.exec_driver_sql("""
        CREATE TABLE packages (
            id INTEGER NOT NULL PRIMARY KEY,
            owner_admin_id INTEGER,
            name VARCHAR(128) NOT NULL,
            quota_gb FLOAT,
            duration_days INTEGER,
            price BIGINT,
            ovpn_template TEXT,
            cooperation_price BIGINT,
            description TEXT,
            enabled BOOLEAN,
            bot_enabled BOOLEAN,
            seller_visible BOOLEAN NOT NULL,
            one_time_per_user BOOLEAN NOT NULL,
            sort_order INTEGER,
            created_at DATETIME,
            max_concurrent_sessions INTEGER,
            speed_limit_mbps INTEGER,
            custom_message TEXT
        )
    """)
    conn.exec_driver_sql(
        "INSERT INTO packages (id, name, quota_gb, duration_days, price, enabled, bot_enabled,"
        " seller_visible, one_time_per_user, owner_admin_id) "
        "VALUES (1, 'قدیمی', 10, 30, 1000, 1, 1, 1, 0, 2)"
    )
check("the old schema really lacks the column",
      "miniapp_enabled" in {c["name"] for c in inspect(old).get_columns("packages")}, False)
check("...and has no groups table", "package_groups" in inspect(old).get_table_names(), False)

# The real startup path, pointed at the old database. It reads a module-
# level engine, so that is what gets swapped - running a copy of the
# migration logic here would prove only that the copy works.
from app import main as app_main  # noqa: E402

models.Base.metadata.create_all(old)   # brand-new TABLES (package_groups)
real_engine, app_main.engine = app_main.engine, old
try:
    app_main._auto_migrate_missing_columns()   # new COLUMNS on existing tables
finally:
    app_main.engine = real_engine
cols = {c["name"] for c in inspect(old).get_columns("packages")}
check("the column was added", "miniapp_enabled" in cols, True)
check("...and group_id too", "group_id" in cols, True)
check("the groups table was created", "package_groups" in inspect(old).get_table_names(), True)
with old.begin() as conn:
    row = conn.execute(text("SELECT miniapp_enabled, group_id, name FROM packages WHERE id=1")).first()
check("the existing package survived untouched", row[2], "قدیمی")
check("...is visible in the Mini App by default", bool(row[0]), True)
check("...and is simply ungrouped", row[1], None)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("قفسه‌ها از هم جدا می‌مانند و هیچ پکیجی بی‌صدا از فروش خارج نمی‌شود")
