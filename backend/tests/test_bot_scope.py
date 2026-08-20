"""The bot and the panel must answer "who may I see" identically.

Run:  python3 backend/tests/test_bot_scope.py

The interesting cases are the ones where a naive IN-list is wrong: the
ownerless customers (577 of them live), and a level-2 Admin's own Sellers'
customers. Both were answered differently by the two code paths before.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import hierarchy
from app.routers import bot as bot_router

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add_admin(db, username, *, superadmin=False, parent=None, role=None):
    row = models.AdminUser(
        username=username, hashed_password="x", is_superadmin=superadmin,
        parent_admin_id=parent.id if parent else None, role=role,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def add_user(db, username, owner):
    row = models.User(username=username, owner_admin_id=owner.id if owner else None)
    db.add(row)
    db.commit()
    return row


def visible(db, admin_id):
    clause = bot_router._visibility_filter(db, admin_id)
    q = db.query(models.User)
    if clause is not None:
        q = q.filter(clause)
    return {u.username for u in q.all()}


db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add_admin(db, "admin1", role=hierarchy.ROLE_ADMIN)
s1 = add_admin(db, "seller1", parent=a1, role=hierarchy.ROLE_SELLER)
a2 = add_admin(db, "admin2", role=hierarchy.ROLE_ADMIN)

add_user(db, "sa_cust", sa)
add_user(db, "a1_cust", a1)
add_user(db, "s1_cust", s1)
add_user(db, "a2_cust", a2)
add_user(db, "orphan", None)

print("--- who sees which customers ---")
# The decision taken this session: the bot matches the panel, so a
# superadmin sees their own plus the ownerless - not everyone's.
check("superadmin: own + ownerless", visible(db, sa.id), {"sa_cust", "orphan"})
check("admin: own + their Seller's", visible(db, a1.id), {"a1_cust", "s1_cust"})
check("seller: only their own", visible(db, s1.id), {"s1_cust"})
check("another admin is fully isolated", visible(db, a2.id), {"a2_cust"})
check("unowned bot (no account behind it) is unfiltered",
      visible(db, None), {"sa_cust", "a1_cust", "s1_cust", "a2_cust", "orphan"})
# An id that matches nothing must see nothing, never everything.
check("unknown id sees nothing", visible(db, 99999), set())

print("\n--- the panel gives the same answers ---")
for admin in (sa, a1, s1, a2):
    panel = {
        u.username
        for u in db.query(models.User).filter(hierarchy.user_visibility_clause(db, admin)).all()
    }
    check(f"{admin.username}: bot == panel", visible(db, admin.id), panel)

print("\n--- single-object lookup agrees with the query ---")
from fastapi import HTTPException


def can_fetch(db, username, admin_id) -> bool:
    try:
        bot_router._get_user_or_404(db, username, admin_id)
        return True
    except HTTPException:
        return False


check("superadmin can fetch an ownerless customer by name",
      can_fetch(db, "orphan", sa.id), True)
check("an admin cannot fetch an ownerless customer",
      can_fetch(db, "orphan", a1.id), False)
check("an admin can fetch their Seller's customer",
      can_fetch(db, "s1_cust", a1.id), True)
check("an admin cannot fetch another admin's customer",
      can_fetch(db, "a2_cust", a1.id), False)
check("a seller cannot fetch their parent's customer",
      can_fetch(db, "a1_cust", s1.id), False)

print("\n--- broadcast recipients are scoped too ---")
# telegram_user_ids returns ids, so give every customer one and map back to
# names - comparing sets of names is what makes a failure readable.
for i, u in enumerate(db.query(models.User).all(), start=1000):
    u.telegram_id = i
db.commit()
by_tg = {u.telegram_id: u.username for u in db.query(models.User).all()}
for name, admin, expected in (
    ("superadmin", sa, {"sa_cust", "orphan"}),
    ("admin1", a1, {"a1_cust", "s1_cust"}),
    ("seller1", s1, {"s1_cust"}),
):
    got = {by_tg[t] for t in bot_router.telegram_user_ids(db=db, owner_admin_id=admin.id)}
    check(f"broadcast from {name}", got, expected)

print("\n--- pending receipts carry an owner ---")
import tempfile
from app.telegram_bot import storage
from app.telegram_bot.config import config

config.db_path = tempfile.mktemp()
storage.init_db()

pkg = {"id": 1, "name": "p", "quota_gb": 10, "duration_days": 30, "price": 1000}
ids = {}
for label, owner in (("sa_req", None), ("a1_req", a1.id), ("s1_req", s1.id), ("a2_req", a2.id)):
    ids[label] = storage.create_pending(
        telegram_id=1, telegram_username=None, telegram_name=None,
        kind="new", package=pkg, target_username=label, owner_admin_id=owner,
    )


def pending_for(admin):
    owner_ids = hierarchy.owned_admin_ids(db, admin)
    return {
        r["target_username"]
        for r in storage.list_pending(owner_ids, bool(admin.is_superadmin))
    }


check("superadmin sees their own + the ownerless request",
      pending_for(sa), {"sa_req"})
check("admin sees their own and their Seller's", pending_for(a1), {"a1_req", "s1_req"})
check("seller sees only their own", pending_for(s1), {"s1_req"})
check("unscoped config admin sees all four",
      {r["target_username"] for r in storage.list_pending(None, True)},
      {"sa_req", "a1_req", "s1_req", "a2_req"})

print("\n--- acting on a single request is re-checked ---")
req = storage.get_pending(ids["a2_req"])
check("an admin cannot approve another admin's request",
      storage.may_handle(req, hierarchy.owned_admin_ids(db, a1), False), False)
check("its real owner can", storage.may_handle(req, hierarchy.owned_admin_ids(db, a2), False), True)
own = storage.get_pending(ids["sa_req"])
check("an ownerless request is the superadmin's",
      storage.may_handle(own, hierarchy.owned_admin_ids(db, sa), True), True)
check("...and nobody else's",
      storage.may_handle(own, hierarchy.owned_admin_ids(db, a1), False), False)
check("the unscoped config admin may handle anything",
      storage.may_handle(req, None, True), True)

os.unlink(config.db_path)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
