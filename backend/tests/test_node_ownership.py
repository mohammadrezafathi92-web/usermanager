"""A granted node may be USED, never reconfigured.

Run:  python3 backend/tests/test_node_ownership.py

AdminNodeAccess means "provision your customers on this server". It never
meant "reconfigure it" - but the gate that was supposed to enforce that
(require_permission("edit_nodes")) referenced a key that no longer exists,
and short-circuits for level-2 Admins anyway. So it enforced nothing on
exactly the role it existed for.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models, schemas
from app.routers import nodes as nodes_router
from app.services import hierarchy

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


db = make_db()
su = models.AdminUser(username="super", hashed_password="x", is_superadmin=True,
                      role=hierarchy.ROLE_SUPERADMIN)
db.add(su)
db.commit()
db.refresh(su)

a1 = models.AdminUser(username="a1", hashed_password="x", parent_admin_id=su.id,
                      role=hierarchy.ROLE_ADMIN)
a2 = models.AdminUser(username="a2", hashed_password="x", parent_admin_id=su.id,
                      role=hierarchy.ROLE_ADMIN)
db.add_all([a1, a2])
db.commit()
db.refresh(a1)
db.refresh(a2)
for row in (su, a1, a2):
    hierarchy.rebuild_path(db, row)
db.commit()

# The superadmin's shared infrastructure, granted to a1 for use.
shared = models.Node(name="shared", type=models.NodeType.mikrotik, owner_admin_id=None)
# a1's own server, added with their own credentials.
own = models.Node(name="a1-own", type=models.NodeType.mikrotik, owner_admin_id=a1.id)
db.add_all([shared, own])
db.commit()
db.refresh(shared)
db.refresh(own)
db.add(models.AdminNodeAccess(admin_id=a1.id, node_id=shared.id))
db.commit()


def resolve(fn, node, admin):
    """(ok, http status). fn is _get_scoped_node or _get_owned_node."""
    try:
        fn(db, node.id, admin)
        return True, None
    except HTTPException as exc:
        return False, exc.status_code


print("--- what an Admin may SEE and USE ---")
check("the granted node is in scope",
      resolve(nodes_router._get_scoped_node, shared, a1), (True, None))
check("their own node is in scope",
      resolve(nodes_router._get_scoped_node, own, a1), (True, None))
check("another Admin's node is not - and 404s, not 403s, so ids cannot be probed",
      resolve(nodes_router._get_scoped_node, own, a2), (False, 404))

print("\n--- what an Admin may CHANGE ---")
check("their own node: yes", resolve(nodes_router._get_owned_node, own, a1), (True, None))
check("a granted node: refused", resolve(nodes_router._get_owned_node, shared, a1), (False, 403))
check("a stranger's node: still 404, not 403",
      resolve(nodes_router._get_owned_node, own, a2), (False, 404))
check("the superadmin may change anything",
      resolve(nodes_router._get_owned_node, shared, su), (True, None))
check("...including an Admin's own node",
      resolve(nodes_router._get_owned_node, own, su), (True, None))

print("\n--- the actual endpoints, not just the helper ---")


def call(fn, node, admin, **kw):
    try:
        fn(node.id, db=db, admin=admin, **kw)
        return True, None
    except HTTPException as exc:
        return False, exc.status_code


payload = schemas.NodeUpdate(name="renamed")
check("editing a granted node is refused",
      call(nodes_router.update_node, shared, a1, payload=payload), (False, 403))
db.rollback()
check("the shared node kept its name",
      db.get(models.Node, shared.id).name, "shared")

check("editing their own node works",
      call(nodes_router.update_node, own, a1, payload=schemas.NodeUpdate(name="mine")),
      (True, None))
check("...and really changed it", db.get(models.Node, own.id).name, "mine")

# The imports are the sharpest edge: on a shared node they would pull other
# admins' customers into the importer's own account.
for label, fn in (
    ("import PPP users", nodes_router.import_ppp_users),
    ("import usermanager users", nodes_router.import_usermanager_users),
    ("rebuild clients", nodes_router.rebuild_node_clients),
    ("import 3x-ui clients", nodes_router.import_3xui_clients),
):
    ok, status = call(fn, shared, a1)
    check(f"{label} on a granted node is refused", (ok, status), (False, 403))

print("\n--- taking a server out of rotation is the superadmin's alone ---")
check("an Admin cannot disable even their OWN node",
      call(nodes_router.update_node, own, a1, payload=schemas.NodeUpdate(enabled=False)),
      (False, 403))
db.rollback()
check("the node is still enabled", db.get(models.Node, own.id).enabled, True)

check("nor a granted one",
      call(nodes_router.update_node, shared, a1, payload=schemas.NodeUpdate(enabled=False)),
      (False, 403))
db.rollback()

# The refusal must be about the switch, not about everything else - an
# Admin renaming their own server has to keep working.
check("an unrelated change to their own node still works",
      call(nodes_router.update_node, own, a1, payload=schemas.NodeUpdate(name="renamed-again")),
      (True, None))
check("...and applied", db.get(models.Node, own.id).name, "renamed-again")

check("the superadmin may disable",
      call(nodes_router.update_node, own, su, payload=schemas.NodeUpdate(enabled=False)),
      (True, None))
check("...and it really is disabled", db.get(models.Node, own.id).enabled, False)
call(nodes_router.update_node, own, su, payload=schemas.NodeUpdate(enabled=True))

print("\n--- a Seller reaches none of it ---")
s1 = models.AdminUser(username="s1", hashed_password="x", parent_admin_id=a1.id,
                      role=hierarchy.ROLE_SELLER)
db.add(s1)
db.commit()
db.refresh(s1)
hierarchy.rebuild_path(db, s1)
db.commit()
check("a Seller cannot even see a node",
      resolve(nodes_router._get_scoped_node, shared, s1), (False, 404))
check("nor change one", resolve(nodes_router._get_owned_node, own, s1), (False, 404))

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
