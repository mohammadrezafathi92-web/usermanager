"""Phase 3: role is an independent field, not a consequence of position.

Run:  python3 backend/tests/test_hierarchy_phase3.py

Uses a real in-memory SQLite database rather than mocks, because the rules
under test are about rows relating to other rows - a mocked session would
be asserting that the test's own idea of the tree is self-consistent.
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

failures: list[str] = []


def check(label: str, got, expected) -> None:
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def check_truthy(label: str, value, want: bool) -> None:
    got = bool(value)
    if got == want:
        print(f"PASS  {label}" + ("" if want else ""))
    else:
        failures.append(label)
        print(f"FAIL  {label}: got {value!r}, expected {'a reason' if want else 'no reason'}")


def make_db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def add(db, username, *, superadmin=False, parent=None, role=None):
    row = models.AdminUser(
        username=username,
        hashed_password="x",
        is_superadmin=superadmin,
        parent_admin_id=parent.id if parent else None,
        role=role,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


# ---------------------------------------------------------------- placement
print("--- validate_placement ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add(db, "admin1", role=hierarchy.ROLE_ADMIN)
s1 = add(db, "seller1", parent=a1, role=hierarchy.ROLE_SELLER)

# The case this whole phase exists for: an Admin whose parent is the
# superadmin. Under the old derive-from-parent rule this was impossible to
# express, and eight live accounts are stuck because of it.
check("admin under superadmin is legal",
      hierarchy.validate_placement(hierarchy.ROLE_ADMIN, sa), None)
check("admin with no parent is legal",
      hierarchy.validate_placement(hierarchy.ROLE_ADMIN, None), None)
check_truthy("admin under another admin is refused",
             hierarchy.validate_placement(hierarchy.ROLE_ADMIN, a1), True)

check("seller under an admin is legal",
      hierarchy.validate_placement(hierarchy.ROLE_SELLER, a1), None)
check("seller under the superadmin is legal",
      hierarchy.validate_placement(hierarchy.ROLE_SELLER, sa), None)
check_truthy("parentless seller is refused",
             hierarchy.validate_placement(hierarchy.ROLE_SELLER, None), True)
check_truthy("seller under a seller is refused",
             hierarchy.validate_placement(hierarchy.ROLE_SELLER, s1), True)
check_truthy("admin under a seller is refused",
             hierarchy.validate_placement(hierarchy.ROLE_ADMIN, s1), True)
check_truthy("unknown role is refused",
             hierarchy.validate_placement("owner", None), True)

# ------------------------------------------------------- node access effect
print("\n--- what the role actually changes ---")
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
node = models.Node(name="n1", type=models.NodeType.mikrotik)
db.add(node)
db.commit()
db.refresh(node)

# Exactly the live situation: an account under the superadmin, holding a
# node grant, classified as a Seller.
stuck = add(db, "reseller", parent=sa, role=hierarchy.ROLE_SELLER)
db.add(models.AdminNodeAccess(admin_id=stuck.id, node_id=node.id))
db.commit()

check("grant is thrown away while the account is a Seller",
      hierarchy.accessible_node_ids(db, stuck), set())

stuck.role = hierarchy.ROLE_ADMIN
db.commit()
check("same grant takes effect once the role says Admin",
      hierarchy.accessible_node_ids(db, stuck), {node.id})
check("and the parent link is untouched by that",
      stuck.parent_admin_id, sa.id)

# ------------------------------------------------------------ the endpoint
print("\n--- reparent endpoint ---")
from fastapi import HTTPException

from app.routers import admins as admins_router
from app import schemas


def reparent(db, target, *, parent_id=None, role=None):
    payload = schemas.AdminReparentRequest(parent_admin_id=parent_id, role=role)
    return admins_router.reparent_admin(target.id, payload, db=db, _s=None)


def reparent_error(db, target, *, parent_id=None, role=None) -> str | None:
    try:
        reparent(db, target, parent_id=parent_id, role=role)
        return None
    except HTTPException as exc:
        return str(exc.detail)


# Patch out the response serialiser - it pulls in unrelated aggregates and
# is not what these assertions are about.
admins_router._out = lambda db, admin: admin  # type: ignore[assignment]

db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
target = add(db, "reseller", parent=sa, role=hierarchy.ROLE_SELLER)

reparent(db, target, parent_id=sa.id, role=hierarchy.ROLE_ADMIN)
db.refresh(target)
check("promoting in place sets the role", target.role, hierarchy.ROLE_ADMIN)
check("promoting in place keeps the parent", target.parent_admin_id, sa.id)

# Omitting the role must not silently change it - the exact bug being fixed.
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
mover = add(db, "mover", parent=sa, role=hierarchy.ROLE_ADMIN)
reparent(db, mover, parent_id=None)
db.refresh(mover)
check("a move without a role leaves the role alone", mover.role, hierarchy.ROLE_ADMIN)
check("a move without a role still moves it", mover.parent_admin_id, None)

# Demotion still rescues the demoted account's own Sellers.
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
keeper = add(db, "keeper", role=hierarchy.ROLE_ADMIN)
victim = add(db, "victim", role=hierarchy.ROLE_ADMIN)
child = add(db, "child", parent=victim, role=hierarchy.ROLE_SELLER)
db.add(models.Package(name="pkg", price=1, owner_admin_id=victim.id))
db.commit()

reparent(db, victim, parent_id=keeper.id, role=hierarchy.ROLE_SELLER)
db.refresh(victim)
db.refresh(child)
check("demoted account becomes a Seller", victim.role, hierarchy.ROLE_SELLER)
check("its own Seller is promoted rather than orphaned", child.role, hierarchy.ROLE_ADMIN)
check("the promoted Seller has no parent", child.parent_admin_id, None)
check("the promoted Seller's path is rebuilt", child.tree_path, f"/{child.id}/")
pkg = db.query(models.Package).first()
check("its packages move to the new parent", pkg.owner_admin_id, keeper.id)

# Demotion under the SUPERADMIN must hand packages to NULL, not to the
# superadmin's numeric id - nothing resolves that id to a visible scope.
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
victim = add(db, "victim", role=hierarchy.ROLE_ADMIN)
db.add(models.Package(name="pkg", price=1, owner_admin_id=victim.id))
db.commit()
reparent(db, victim, parent_id=sa.id, role=hierarchy.ROLE_SELLER)
pkg = db.query(models.Package).first()
check("demoted under the superadmin, packages become global (NULL)", pkg.owner_admin_id, None)

# Promoting an Admin under the superadmin must NOT scatter its Sellers -
# the old code cascaded on "gained a parent", which would have.
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
boss = add(db, "boss", role=hierarchy.ROLE_ADMIN)
kid = add(db, "kid", parent=boss, role=hierarchy.ROLE_SELLER)
reparent(db, boss, parent_id=sa.id, role=hierarchy.ROLE_ADMIN)
db.refresh(kid)
check("moving an Admin under the superadmin keeps its Sellers", kid.parent_admin_id, boss.id)
check("and keeps their role", kid.role, hierarchy.ROLE_SELLER)

# Refusals
db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)
a1 = add(db, "a1", role=hierarchy.ROLE_ADMIN)
a2 = add(db, "a2", role=hierarchy.ROLE_ADMIN)
s1 = add(db, "s1", parent=a1, role=hierarchy.ROLE_SELLER)

check_truthy("admin under another admin refused by the endpoint",
             reparent_error(db, a2, parent_id=a1.id, role=hierarchy.ROLE_ADMIN), True)
check_truthy("parentless seller refused by the endpoint",
             reparent_error(db, s1, parent_id=None, role=hierarchy.ROLE_SELLER), True)
check_truthy("own-descendant parent refused",
             reparent_error(db, a1, parent_id=s1.id, role=hierarchy.ROLE_SELLER), True)
check_truthy("self as parent refused",
             reparent_error(db, a1, parent_id=a1.id, role=hierarchy.ROLE_ADMIN), True)
check_truthy("missing parent refused",
             reparent_error(db, s1, parent_id=9999, role=hierarchy.ROLE_SELLER), True)

# ---------------------------------------------------------------- creation
# The same expressiveness has to exist at CREATE time, or an Admin under the
# superadmin must still be created wrong and then promoted.
print("\n--- create_admin ---")


def create(db, creator, username, *, parent_id=None, role=None):
    payload = schemas.AdminCreate(
        username=username, password="pw12345678",
        parent_admin_id=parent_id, role=role,
    )
    return admins_router.create_admin(payload, db=db, current=creator)


db = make_db()
sa = add(db, "super", superadmin=True, role=hierarchy.ROLE_SUPERADMIN)

create(db, sa, "newadmin", parent_id=sa.id, role=hierarchy.ROLE_ADMIN)
row = db.query(models.AdminUser).filter_by(username="newadmin").one()
check("superadmin can create an Admin under themselves", row.role, hierarchy.ROLE_ADMIN)
check("...and it really is parented to them", row.parent_admin_id, sa.id)

create(db, sa, "newseller", parent_id=sa.id, role=hierarchy.ROLE_SELLER)
row = db.query(models.AdminUser).filter_by(username="newseller").one()
check("superadmin can still create a Seller under themselves", row.role, hierarchy.ROLE_SELLER)

# Omitting the role must behave exactly as before this phase.
create(db, sa, "legacy", parent_id=sa.id)
row = db.query(models.AdminUser).filter_by(username="legacy").one()
check("omitted role falls back to the old positional guess", row.role, hierarchy.ROLE_SELLER)

# A level-2 Admin must not be able to mint a peer by passing role="admin".
boss = db.query(models.AdminUser).filter_by(username="newadmin").one()
create(db, boss, "sneaky", role=hierarchy.ROLE_ADMIN)
row = db.query(models.AdminUser).filter_by(username="sneaky").one()
check("a level-2 Admin cannot create another Admin", row.role, hierarchy.ROLE_SELLER)
check("...their new account is parented to them", row.parent_admin_id, boss.id)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
