"""One broken legacy customer must not undo another customer's conversion."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models
from app.services import purchase_migration
import _mariadb_scratch as scratch


def make_db(*, mixed=False, engine=None):
    engine = engine or create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    node = models.Node(name="migration-node", type=models.NodeType.mikrotik)
    package = models.Package(name="migration-package", price=1, quota_gb=10)
    settings = models.PanelSettings(id=1, legacy_purchases_migrated=False)
    db.add_all([node, package, settings])
    db.flush()
    for name in ("good-before", "bad", "good-after"):
        user = models.User(username=name, package_id=package.id,
                           total_quota_bytes=10 * 1024 ** 3)
        db.add(user)
        db.flush()
        db.add(models.Connection(user_id=user.id, node_id=node.id,
                                 type=models.ConnectionType.wireguard))
        if mixed:
            db.add(models.Purchase(user_id=user.id, package_id=package.id,
                                   quota_bytes=10 * 1024 ** 3))
    db.commit()
    return db


def check(db, *, mixed=False, failed_remains=True):
    db.expire_all()
    users = {u.username: u for u in db.query(models.User).all()}
    for name in ("good-before", "good-after"):
        assert len(users[name].purchases) == (2 if mixed else 1), name
        assert all(c.purchase_id is not None for c in users[name].connections), name
    if failed_remains:
        assert len(users["bad"].purchases) == (1 if mixed else 0)
        assert all(c.purchase_id is None for c in users["bad"].connections)
    else:
        assert len(users["bad"].purchases) == (2 if mixed else 1)
        assert all(c.purchase_id is not None for c in users["bad"].connections)


original_migrate = purchase_migration.migrate_user


def flaky_migrate(db, user):
    result = original_migrate(db, user)
    if user.username == "bad":
        raise RuntimeError("injected failure after inserting Purchase")
    return result


purchase_migration.migrate_user = flaky_migrate
try:
    db = make_db()
    assert purchase_migration.migrate_if_needed(db) == (2, 0)
    check(db)
    assert not db.get(models.PanelSettings, 1).legacy_purchases_migrated
finally:
    purchase_migration.migrate_user = original_migrate

assert purchase_migration.migrate_if_needed(db) == (1, 0)
check(db, failed_remains=False)
assert db.get(models.PanelSettings, 1).legacy_purchases_migrated
assert purchase_migration.migrate_if_needed(db) == (0, 0)
db.close()

purchase_migration.migrate_user = flaky_migrate
try:
    db = make_db()
    assert purchase_migration.migrate_legacy_only_users(db) == (2, 0)
    check(db)
finally:
    purchase_migration.migrate_user = original_migrate

assert purchase_migration.migrate_legacy_only_users(db) == (1, 0)
check(db, failed_remains=False)
db.close()

original_absorb = purchase_migration.user_ops_absorb


def flaky_absorb(db, user):
    result = original_absorb(db, user)
    if user.username == "bad":
        raise RuntimeError("injected failure after inserting Purchase")
    return result


purchase_migration.user_ops_absorb = flaky_absorb
try:
    db = make_db(mixed=True)
    assert purchase_migration.fix_mixed_users(db) == 2
    check(db, mixed=True)
finally:
    purchase_migration.user_ops_absorb = original_absorb

assert purchase_migration.fix_mixed_users(db) == 1
check(db, mixed=True, failed_remains=False)
db.close()

url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        db = make_db(engine=engine)
        purchase_migration.migrate_user = flaky_migrate
        try:
            assert purchase_migration.migrate_if_needed(db) == (2, 0)
            check(db)
            assert not db.get(models.PanelSettings, 1).legacy_purchases_migrated
        finally:
            purchase_migration.migrate_user = original_migrate
        assert purchase_migration.migrate_if_needed(db) == (1, 0)
        check(db, failed_remains=False)
        assert db.get(models.PanelSettings, 1).legacy_purchases_migrated
        db.close()
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB for migration savepoints")

print("purchase migration isolates failed customers and retries them")
