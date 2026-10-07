"""One-time claim identity, rollback, binding and real two-session races."""
import datetime as dt
import hashlib
import os
import sys
import tempfile
import threading
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from app import models, models_provisioning as mp
from app.services import provisioning_schema, provisioning_claims as claims
from app.services import provisioning_compensation, provisioning_transitions as transitions, receipt_void_schema


def refused(code, callback):
    try:
        callback()
        raise AssertionError("unexpected success")
    except HTTPException as exc:
        assert exc.detail == code, (exc.detail, code)


assert claims.claim_key("user", user_id=7) == hashlib.sha256(b"user|7").hexdigest()
assert claims.claim_key("telegram", tenant_scope_key="a|b", telegram_id=123) == hashlib.sha256(b"telegram|3|a|b|123").hexdigest()
assert claims.claim_key("telegram", tenant_scope_key="admin:1", telegram_id=123) != claims.claim_key(
    "telegram", tenant_scope_key="admin:2", telegram_id=123)
refused("one_time_claim_identity_invalid", lambda: claims.claim_key("user", user_id=True))
refused("one_time_claim_identity_invalid", lambda: claims.claim_key("telegram", tenant_scope_key="x" * 65, telegram_id=1))


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    user = models.User(username="user", telegram_id=123)
    sibling = models.User(username="sibling", telegram_id=123)
    package = models.Package(name="one-time", one_time_per_user=True)
    db.add_all([user, sibling, package])
    db.commit()
    uid, sibling_id, pid = user.id, sibling.id, package.id
    def operation(user_id=uid, **extra):
        values = dict(operation_type="purchase", business_key=str(uuid.uuid4()), request_hash="0" * 64,
            tenant_scope_key="shared", actor_kind="system", intent="{}", state="prepared", target_user_id=user_id,
            wallet_epoch_at_start=0, forward_deadline=dt.datetime.utcnow() + dt.timedelta(minutes=5))
        values.update(extra)
        row = mp.ProvisioningOperation(**values)
        db.add(row)
        db.flush()
        return row
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))
    op = operation()
    rows = claims.reserve(db, op.id, pid)
    assert len(rows) == 2 and {row.claim_kind for row in rows} == {"user", "telegram"}
    assert all(claims.valid(row) for row in rows) and commits == []
    assert {row.id for row in claims.reserve(db, op.id, pid)} == {row.id for row in rows}
    db.rollback()
    assert db.query(mp.OneTimePackageClaim).count() == 0
    db.rollback()
    op = operation()
    oid = op.id
    claims.reserve(db, oid, pid)
    db.commit()
    corrupted = db.query(mp.OneTimePackageClaim).filter_by(operation_id=oid, claim_kind="user").one()
    corrupted.claim_user_id = sibling_id
    db.flush()
    refused("one_time_claim_corrupt", lambda: claims.reserve(db, oid, pid))
    db.rollback()
    other = operation(sibling_id)
    refused("one_time_package_already_claimed", lambda: claims.reserve(db, other.id, pid))
    db.rollback()
    transitions.begin_compensation(db, oid, "node_unavailable")
    db.commit()
    provisioning_compensation.finish(db, oid, db.get(mp.ProvisioningOperation, oid).version)
    db.commit()
    op = operation()
    rows = claims.reserve(db, op.id, pid)
    assert len(rows) == 2 and all(row.release_seq == 0 for row in rows)
    db.commit()
    oid = op.id
    transitions.mark_remote_complete(db, oid)
    purchase = models.Purchase(user_id=uid, package_id=pid, quota_bytes=100)
    db.add(purchase)
    db.flush()
    active = db.query(mp.OneTimePackageClaim).filter_by(operation_id=oid).first()
    active.release_seq, active.released_at = active.id, dt.datetime.utcnow()
    db.flush()
    refused("one_time_claim_purchase_mismatch", lambda: claims.bind_purchase(db, oid, purchase.id))
    db.rollback()
    transitions.mark_remote_complete(db, oid)
    purchase = models.Purchase(user_id=uid, package_id=pid, quota_bytes=100)
    db.add(purchase)
    db.flush()
    claims.bind_purchase(db, oid, purchase.id)
    db.rollback()
    assert all(row.purchase_id is None for row in db.query(mp.OneTimePackageClaim).filter_by(operation_id=oid))
    db.rollback()
    transitions.mark_remote_complete(db, oid)
    purchase = models.Purchase(user_id=uid, package_id=pid, quota_bytes=100)
    db.add(purchase)
    db.flush()
    bound_id = purchase.id
    claims.bind_purchase(db, oid, bound_id)
    db.commit()
    assert all(row.purchase_id == bound_id for row in claims.bind_purchase(db, oid, bound_id))
    db.rollback()
    op = operation(sibling_id)
    refused("one_time_package_already_purchased", lambda: claims.reserve(db, op.id, pid))
    db.rollback()
    op = operation(tenant_scope_key="admin:7")
    refused("one_time_claim_identity_mismatch", lambda: claims.reserve(db, op.id, pid))
    db.rollback()
    op = operation()
    refused("one_time_claim_identity_mismatch", lambda: claims.reserve(db, op.id, pid, telegram_id=456))
    db.rollback()
    print("PASS", engine.dialect.name, "canonical identity, retry, rollback, compensated release, exact purchase binding and sibling reuse refusal")

    # New-user T1 has no User row yet: bind the eventual User by frozen
    # username and Telegram claim, not by an arbitrary caller-supplied ID.
    package2 = models.Package(name="create-once", one_time_per_user=True)
    db.add(package2)
    db.flush()
    op = operation(None, operation_type="create_user", username_claim="new-user")
    claims.reserve(db, op.id, package2.id, telegram_id=555)
    db.commit()
    refused("one_time_claim_request_changed", lambda: claims.reserve(db, op.id, package2.id, telegram_id=556))
    db.rollback()
    transitions.mark_remote_complete(db, op.id)
    new_user = models.User(username="new-user", telegram_id=555)
    db.add(new_user)
    db.flush()
    purchase = models.Purchase(user_id=new_user.id, package_id=package2.id, quota_bytes=100)
    db.add(purchase)
    db.flush()
    claims.bind_purchase(db, op.id, purchase.id)
    db.commit()
    print("PASS", engine.dialect.name, "new-user claim binds only frozen username and Telegram identity")

    op = operation()
    transitions.mark_remote_complete(db, op.id)
    purchase = models.Purchase(user_id=uid, package_id=pid, quota_bytes=100)
    db.add(purchase)
    db.flush()
    refused("one_time_claim_missing", lambda: claims.bind_purchase(db, op.id, purchase.id))
    db.rollback()

    # The same Telegram user in a different reseller tree is not blocked
    # by another tenant's historical Purchase.
    admins = [models.AdminUser(username="tenant-a", hashed_password="x"),
              models.AdminUser(username="tenant-b", hashed_password="x")]
    tenant_package = models.Package(name="tenant-once", one_time_per_user=True)
    db.add_all(admins + [tenant_package])
    db.flush()
    tenant_users = [models.User(username="tenant-user-a", telegram_id=999, owner_admin_id=admins[0].id),
                    models.User(username="tenant-user-b", telegram_id=999, owner_admin_id=admins[1].id)]
    db.add_all(tenant_users)
    db.flush()
    db.add(models.Purchase(user_id=tenant_users[0].id, package_id=tenant_package.id, quota_bytes=100))
    db.commit()
    op = operation(tenant_users[1].id, tenant_scope_key=f"admin:{admins[1].id}")
    rows = claims.reserve(db, op.id, tenant_package.id)
    assert len(rows) == 2 and all(claims.valid(row) for row in rows)
    db.rollback()
    print("PASS", engine.dialect.name, "historical purchases and Telegram claims remain tenant-scoped")

    race_package = models.Package(name="race", one_time_per_user=True)
    db.add(race_package)
    db.flush()
    operations = [operation(ident) for ident in (uid, sibling_id)]
    db.commit()
    ids, race_pid = [row.id for row in operations], race_package.id
    db.close()
    barrier = threading.Barrier(2)
    results, errors = [], []
    def compete(oid):
        session = Factory()
        try:
            barrier.wait(timeout=10)
            if engine.dialect.name == "sqlite":
                session.execute(text("BEGIN IMMEDIATE"))
            try:
                claims.reserve(session, oid, race_pid)
                session.commit()
                results.append("won")
            except HTTPException as exc:
                assert exc.detail == "one_time_package_already_claimed"
                session.rollback()
                results.append("refused")
        except Exception as exc:
            errors.append(type(exc).__name__)
            session.rollback()
        finally:
            session.close()
    threads = [threading.Thread(target=compete, args=(oid,)) for oid in ids]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and sorted(results) == ["refused", "won"] and not any(thread.is_alive() for thread in threads), (results, errors)
    with Factory() as db:
        rows = db.query(mp.OneTimePackageClaim).filter_by(package_id=race_pid, release_seq=0).all()
        assert len(rows) == 2 and len({row.operation_id for row in rows}) == 1
    print("PASS", engine.dialect.name, "two real sessions, two sibling accounts: exactly one complete claim set")


with tempfile.TemporaryDirectory(prefix="um-one-time-claims-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
