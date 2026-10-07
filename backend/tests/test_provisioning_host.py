"""Host secret handling and uncached ownership fencing; no real nodes."""
import datetime as dt
import hashlib
import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app import models_provisioning as mp
from app.services import provisioning_schema, provisioning_host as host


def refuses(kind, callback):
    try:
        callback()
        raise AssertionError("unexpected acceptance")
    except kind:
        pass


with tempfile.TemporaryDirectory(prefix="um-host-identity-") as directory:
    secret = Path(directory) / "host.id"
    boot = Path(directory) / "boot.id"
    boot.write_text(str(uuid.uuid4()), encoding="ascii")
    refuses(host.HostIdentityUnavailable, lambda: host.read_identity(secret, boot))
    assert not secret.exists()
    secret.write_bytes(b"s" * 32)
    secret.chmod(0o600)
    identity = host.read_identity(secret, boot)
    assert identity.host_id == hashlib.sha256(b"um-host|" + b"s" * 32).hexdigest()
    assert "s" * 32 not in repr(identity)
    assert host.read_identity(secret, boot) == identity
    secret.chmod(0o644)
    refuses(host.HostIdentityUnavailable, lambda: host.read_identity(secret, boot))
    secret.chmod(0o600)
    linked = Path(directory) / "link"
    linked.symlink_to(secret)
    refuses(host.HostIdentityUnavailable, lambda: host.read_identity(linked, boot))
    secret.write_bytes(b"short")
    refuses(host.HostIdentityUnavailable, lambda: host.read_identity(secret, boot))
    secret.write_bytes(b"s" * 32)
    boot.write_text("not-a-boot-id", encoding="ascii")
    refuses(host.HostIdentityUnavailable, lambda: host.read_identity(secret, boot))
    print("PASS secure identity: missing, permissive, symlink, short secret and malformed boot fail closed")


def scenario(engine):
    Factory = sessionmaker(bind=engine, autoflush=False)
    assert provisioning_schema.bootstrap(engine, Factory)["ready"]
    db = Factory()
    row = db.get(mp.ProvisioningRuntimeState, 1)
    installation = row.installation_uuid
    identity = host.HostIdentity("a" * 64, str(uuid.uuid4()))
    row.owner_state = "active"
    row.owner_host_id = identity.host_id
    row.owner_boot_id = identity.boot_id
    row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = dt.datetime.utcnow()
    row.ownership_epoch = 1
    row.lock_backend = "flock"
    db.commit()
    statements = []
    listener = lambda conn, cursor, statement, parameters, context, many: statements.append(statement)
    event.listen(engine, "before_cursor_execute", listener)
    assert host.revalidate(engine, identity, 1, installation)["ownership_epoch"] == 1
    assert statements and all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    event.remove(engine, "before_cursor_execute", listener)
    refuses(host.OwnershipMismatch, lambda: host.revalidate(engine,
        host.HostIdentity("b" * 64, identity.boot_id), 1, installation))
    refuses(host.OwnershipMismatch, lambda: host.revalidate(engine,
        host.HostIdentity(identity.host_id, str(uuid.uuid4())), 1, installation))
    refuses(host.OwnershipMismatch, lambda: host.revalidate(engine, identity, 1, str(uuid.uuid4())))
    # A stale heartbeat NEVER transfers ownership or authorizes another boot.
    row.owner_heartbeat_at = dt.datetime.utcnow() - dt.timedelta(days=30)
    db.commit()
    assert host.revalidate(engine, identity, 1, installation)["owner_host_id"] == identity.host_id
    row.ownership_epoch = 2
    db.commit()
    refuses(host.OwnershipMismatch, lambda: host.revalidate(engine, identity, 1, installation))
    assert host.revalidate(engine, identity, 2, installation)["ownership_epoch"] == 2
    row.owner_state = "draining"
    db.commit()
    refuses(host.OwnershipMismatch, lambda: host.revalidate(engine, identity, 2, installation))
    print("PASS", engine.dialect.name, "fresh read-only ownership rejects wrong host, boot, installation, epoch and drain; no heartbeat takeover")
    db.close()


with tempfile.TemporaryDirectory(prefix="um-host-db-") as directory:
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
