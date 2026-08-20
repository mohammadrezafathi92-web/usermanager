"""Deleting an account must never orphan anything that has a parent.

Run:  python3 backend/tests/test_safe_delete.py
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
from app.routers import admins as admins_router

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
    db.add(models.User(username=username, owner_admin_id=owner.id if owner else None))
    db.commit()


def owner_of(db, username):
    return db.query(models.User).filter_by(username=username).one().owner_admin_id


def delete(db, target, actor):
    return admins_router.delete_admin(target.id, db=db, current=actor, _confirm=None)


# ---------------------------------------------- a Seller's customers go up
print("--- deleting a Seller ---")
db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add_admin(db, "boss", role=hierarchy.ROLE_ADMIN)
seller = add_admin(db, "seller", parent=boss, role=hierarchy.ROLE_SELLER)
add_user(db, "cust", seller)
db.add(models.Package(name="pkg", price=1, owner_admin_id=seller.id))
db.commit()

delete(db, seller, sa)
# This is the case that most likely produced the 577: the parent Admin was
# right there and the customer was cut loose anyway.
check("customer is inherited by the parent Admin", owner_of(db, "cust"), boss.id)
check("package is inherited too",
      db.query(models.Package).one().owner_admin_id, boss.id)
check("no ownerless customers were created",
      db.query(models.User).filter(models.User.owner_admin_id.is_(None)).count(), 0)

# ------------------------------------------- an Admin under the superadmin
print("\n--- deleting an Admin that sits under the superadmin ---")
db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
mid = add_admin(db, "mid", parent=sa, role=hierarchy.ROLE_ADMIN)
kid = add_admin(db, "kid", parent=mid, role=hierarchy.ROLE_SELLER)
add_user(db, "mid_cust", mid)
add_user(db, "kid_cust", kid)
db.add(models.Package(name="pkg", price=1, owner_admin_id=mid.id))
db.commit()

delete(db, mid, sa)
db.refresh(kid)
check("its Seller moves under the superadmin", kid.parent_admin_id, sa.id)
check("...and stays a Seller, because it still has a parent",
      kid.role, hierarchy.ROLE_SELLER)
check("its Seller's path is rebuilt", kid.tree_path, f"/{sa.id}/{kid.id}/")
check("its own customers go to the superadmin", owner_of(db, "mid_cust"), sa.id)
check("its Seller's customers are untouched", owner_of(db, "kid_cust"), kid.id)
# A superadmin owns packages as NULL, never by id - handing over the
# numeric id would put them in a scope nothing resolves.
check("packages become global rather than id-owned",
      db.query(models.Package).one().owner_admin_id, None)

# ------------------------------------------------------- deleting a root
print("\n--- deleting a root Admin (nowhere to inherit) ---")
db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
root = add_admin(db, "root", role=hierarchy.ROLE_ADMIN)
kid = add_admin(db, "kid", parent=root, role=hierarchy.ROLE_SELLER)
add_user(db, "cust", root)
db.commit()

delete(db, root, sa)
db.refresh(kid)
check("its Seller becomes a root Admin", kid.parent_admin_id, None)
check("...and is promoted, since a parentless Seller has no scope",
      kid.role, hierarchy.ROLE_ADMIN)
check("customers fall back to the superadmin pool (NULL)", owner_of(db, "cust"), None)

# ---------------------------------------------------------------- preview
print("\n--- the preview matches what delete actually does ---")
db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add_admin(db, "boss", parent=sa, role=hierarchy.ROLE_ADMIN)
s1 = add_admin(db, "s1", parent=boss, role=hierarchy.ROLE_SELLER)
s2 = add_admin(db, "s2", parent=boss, role=hierarchy.ROLE_SELLER)
for i in range(3):
    add_user(db, f"c{i}", boss)
db.add(models.Package(name="pkg", price=1, owner_admin_id=boss.id))
db.commit()

impact = admins_router.delete_impact(boss.id, db=db, current=sa)
check("preview counts the customers", impact["customers"], 3)
check("preview counts the packages", impact["packages"], 1)
check("preview names the heir", impact["heir_username"], "super")
check("preview lists both Sellers", sorted(c["username"] for c in impact["children"]), ["s1", "s2"])
check("preview says they are NOT promoted (an heir exists)",
      [c["promoted"] for c in impact["children"]], [False, False])

delete(db, boss, sa)
db.refresh(s1)
check("and reality agrees about the Sellers", s1.parent_admin_id, sa.id)
check("and about the customers",
      db.query(models.User).filter(models.User.owner_admin_id == sa.id).count(), 3)

# A root's preview must warn that its Sellers WILL be promoted.
db = make_db()
sa = add_admin(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
root = add_admin(db, "root", role=hierarchy.ROLE_ADMIN)
add_admin(db, "kid", parent=root, role=hierarchy.ROLE_SELLER)
impact = admins_router.delete_impact(root.id, db=db, current=sa)
check("a root's preview has no heir", impact["heir_username"], None)
check("...and warns the Seller will be promoted",
      [c["promoted"] for c in impact["children"]], [True])

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
