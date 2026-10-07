"""DB/file binding is read-only, fresh and never repairs a restore silently."""
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
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker
from app.services import provisioning_schema as schema, provisioning_installation as installation


def refused(code, callback):
    try:
        callback()
        raise AssertionError("installation unexpectedly accepted")
    except installation.InstallationUnavailable as exc:
        assert str(exc) == code, (str(exc), code)


def scenario(engine, directory):
    assert schema.bootstrap(engine, sessionmaker(bind=engine))["ready"]
    with engine.connect() as connection:
        before = dict(connection.execute(text("SELECT * FROM provisioning_runtime_state")).mappings().one())
    path = Path(directory) / "installation.id"
    identity = before["installation_uuid"]
    read = lambda: installation.require_match(engine, base_dir=directory)
    refused("installation_file_missing", read)
    assert not path.exists()
    statements = []
    def collect(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, "before_cursor_execute", collect)
    try:
        for raw in (identity, identity + "\n"):
            path.write_text(raw, encoding="ascii")
            path.chmod(0o600)
            assert read() == identity
            assert path.read_text() == raw
        path.write_text(str(uuid.uuid4()))
        refused("installation_mismatch", read)  # Off is not an exception.
        for raw in (identity.upper(), " " + identity, identity + "\n\n", "x" * 36):
            path.write_text(raw)
            refused("installation_file_invalid", read)
        path.write_text(identity)
        path.chmod(0o666)
        refused("installation_file_invalid", read)
        path.chmod(0o600)
        path.unlink()
        source = Path(directory) / "other-installation.id"
        source.write_text(identity)
        path.symlink_to(source)
        refused("installation_file_invalid", read)
        assert source.read_text() == identity
        path.unlink()
        path.mkdir()
        refused("installation_file_invalid", read)
        path.rmdir()
        path.write_text(identity)
        path.chmod(0o600)
        assert read() == identity
        assert statements and all(statement.lstrip().upper().startswith("SELECT ") for statement in statements)
    finally:
        event.remove(engine, "before_cursor_execute", collect)
    with engine.connect() as connection:
        assert dict(connection.execute(text("SELECT * FROM provisioning_runtime_state")).mappings().one()) == before
    # A later DB restore is noticed, not hidden by a successful cached read.
    with engine.begin() as connection:
        connection.execute(text("UPDATE provisioning_runtime_state SET installation_uuid=:id WHERE id=1"),
            dict(id=str(uuid.uuid4())))
    refused("installation_mismatch", read)
    assert path.read_text() == identity
    print("PASS", engine.dialect.name, "fresh namespace binding, mismatch, missing, malformed, symlink, permissions, read-only")


with tempfile.TemporaryDirectory(prefix="um-installation-") as directory:
    local = Path(directory) / "local"
    local.mkdir()
    engine = create_engine("sqlite:///" + str(Path(directory) / "test.db"))
    try:
        scenario(engine, str(local))
    finally:
        engine.dispose()
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        remote = Path(directory) / "remote"
        remote.mkdir()
        engine = scratch.claim(url)
        try:
            scenario(engine, str(remote))
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
