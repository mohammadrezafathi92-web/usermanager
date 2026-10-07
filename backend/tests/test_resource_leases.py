"""DB-clock lease acquisition, fencing and actual competing sessions."""
import datetime as dt
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv
from app.services import receipt_void_schema, resource_leases as locks


def refuses(kind, callback):
    try:
        callback()
        raise AssertionError("unexpected acceptance")
    except kind:
        pass


def scenario(engine):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert receipt_void_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))
    token = locks.acquire(db, "customer_identity:1", "op:1:1")
    assert token.epoch == 1 and commits == []
    db.rollback()
    assert db.execute(select(rv.resource_locks)).first() is None
    db.rollback()
    token = locks.acquire(db, "customer_identity:1", "op:1:1")
    db.commit()
    refuses(locks.LeaseBusy, lambda: locks.acquire(db, token.resource_key, "op:1:2"))
    db.rollback()
    refuses(locks.LeaseProtocolError, lambda: locks.revalidate(db, [token]))
    db.rollback()
    locks.begin_business(db)
    locks.revalidate(db, [token])
    db.commit()
    refuses(locks.LeaseProtocolError, lambda: locks.revalidate(db, [token]))
    db.rollback()
    locks.renew(db, token, 30)
    db.commit()
    # SQL fixture expires the lease; implementation compares only DB_NOW.
    db.execute(rv.resource_locks.update().where(rv.resource_locks.c.resource_key == token.resource_key)
               .values(leased_until=dt.datetime(2000, 1, 1)))
    db.commit()
    refuses(locks.LeaseLost, lambda: locks.renew(db, token))
    db.rollback()
    locks.begin_business(db)
    refuses(locks.LeaseLost, lambda: locks.revalidate(db, [token]))
    db.rollback()
    replacement = locks.acquire(db, token.resource_key, "op:1:2")
    db.commit()
    assert replacement.epoch == token.epoch + 1
    assert not locks.release(db, token)
    db.commit()
    locks.begin_business(db)
    refuses(locks.LeaseLost, lambda: locks.revalidate(db, [token]))
    db.rollback()
    locks.begin_business(db)
    locks.revalidate(db, [replacement])
    db.commit()
    assert locks.release(db, replacement)
    db.commit()
    assert not locks.release(db, replacement)
    db.commit()
    refuses(locks.LeaseProtocolError, lambda: locks.acquire(db, "../../bad", "owner"))
    refuses(locks.LeaseProtocolError, lambda: locks.acquire(db, "customer_identity:1", "owner", 0))
    db.rollback()
    print("PASS", engine.dialect.name, "rollback/no hidden commit, DB-clock expiry, renewal, epoch fencing and stale release")
    db.close()

    barrier = threading.Barrier(2)
    winners, errors = [], []
    def compete(owner):
        session = Factory()
        try:
            barrier.wait(timeout=10)
            try:
                token = locks.acquire(session, "provisioning_op:2", owner)
                session.commit()
                winners.append(token)
            except locks.LeaseBusy:
                session.rollback()
        except Exception as exc:
            errors.append(type(exc).__name__)
            session.rollback()
        finally:
            session.close()
    threads = [threading.Thread(target=compete, args=(owner,)) for owner in ("op:2:1", "op:2:2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not errors and len(winners) == 1 and not any(thread.is_alive() for thread in threads), (errors, winners)
    db = Factory()
    row = db.execute(select(rv.resource_locks).where(rv.resource_locks.c.resource_key == "provisioning_op:2")).mappings().one()
    assert (row["lease_owner"], row["fencing_epoch"]) == (winners[0].owner, 1)
    db.close()
    print("PASS", engine.dialect.name, "two actual sessions acquire exactly one lease")


with tempfile.TemporaryDirectory(prefix="um-leases-") as directory:
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
