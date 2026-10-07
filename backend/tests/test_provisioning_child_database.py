"""Actual read-only child connections and closed SQL, SQLite + mandatory MariaDB."""
import ast
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app import models
from app.services import provisioning_schema as schema, provisioning_child_database as child
from app.services import gate_locks

tree = ast.parse(Path(child.__file__).read_text())
assert not any(isinstance(node, ast.ImportFrom) and node.module and (
    "models" in node.module or node.module in ("database", "config", "sqlalchemy.orm")) for node in ast.walk(tree))
assert not any(isinstance(node, ast.Name) and node.id in ("SessionLocal", "Session", "Query") for node in ast.walk(tree))


def refuses(callback):
    try:
        callback()
        raise AssertionError("unexpected child database access")
    except child.ChildDatabaseUnavailable:
        pass


def scenario(engine):
    models.Base.metadata.create_all(engine)
    assert schema.bootstrap(engine, sessionmaker(bind=engine))["ready"]
    db = sessionmaker(bind=engine)()
    node = models.Node(name="readonly-node", type=models.NodeType.mikrotik, mt_client_subnet="10.80.0.0/24")
    db.add(node)
    db.commit()
    node_id = node.id
    reader = child.ChildDatabase(engine.url)
    try:
        with reader.connect() as connection:
            installation = connection.execute(text(child.READ_INSTALLATION)).scalar_one()
            assert connection.execute(text(child.READ_RUNTIME)).mappings().one()["gate_mode"] == "off"
            assert connection.execute(text(child.READ_SUBNET), dict(node_id=node_id)).scalar_one() == "10.80.0.0/24"
            assert not hasattr(connection, "commit") and not hasattr(connection, "exec_driver_sql")
            for sql in ("SELECT 1", "SELECT mt_password FROM nodes", "SELECT * FROM users",
                    "SELECT mt_client_subnet FROM nodes WHERE id=:node_id FOR UPDATE",
                    "SELECT installation_uuid FROM provisioning_runtime_state WHERE id = 1; DELETE FROM nodes",
                    "INSERT INTO nodes (name,type) VALUES ('unsafe','mikrotik')",
                    "UPDATE nodes SET name='unsafe'", "DELETE FROM nodes", "DROP TABLE nodes",
                    "ALTER TABLE nodes ADD COLUMN unsafe INT", "CREATE TABLE unsafe (id INT)",
                    "SET SESSION TRANSACTION READ WRITE", "PRAGMA query_only=OFF", "ATTACH ':memory:' AS unsafe"):
                refuses(lambda sql=sql: connection.execute(text(sql), dict(node_id=node_id)))
                refuses(lambda sql=sql: connection._connection.exec_driver_sql(sql))
            # Bypass the Python statement guard ONLY in this adversarial test,
            # proving the underlying database itself also refuses a data write.
            cursor = connection._connection.connection.driver_connection.cursor()
            try:
                try:
                    cursor.execute("UPDATE nodes SET name='unsafe' WHERE id=" + str(node_id))
                    raise AssertionError("underlying database was writable")
                except Exception as exc:
                    if isinstance(exc, AssertionError):
                        raise
                    if engine.dialect.name == "sqlite":
                        assert getattr(exc, "sqlite_errorcode", None) in (8, 23)
                    else:
                        assert exc.args[0] == 1792  # Cannot execute statement in a READ ONLY transaction.
            finally:
                cursor.close()
        node.mt_client_subnet = "10.80.0.0/23"
        db.commit()
        with reader.connect() as fresh:
            assert fresh.execute(text(child.READ_SUBNET), dict(node_id=node_id)).scalar_one() == "10.80.0.0/23"
        assert db.get(models.Node, node_id).name == "readonly-node"
        if engine.dialect.name != "sqlite":
            name = gate_locks.get_lock_name(installation, node_id)
            first, second = reader.connect(), reader.connect()
            try:
                assert first.execute(text(child.ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() == 1
                assert first.execute(text(child.ADVISORY_CHECK), dict(name=name)).scalar_one() == 1
                assert second.execute(text(child.ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() == 0
                first.close()  # NullPool disconnects, no pooled connection can retain the lock.
                assert second.execute(text(child.ADVISORY_GET), dict(name=name, timeout=0)).scalar_one() == 1
                assert second.execute(text(child.ADVISORY_RELEASE), dict(name=name)).scalar_one() == 1
            finally:
                first.close()
                second.close()
        print("PASS", engine.dialect.name, "read-only database, closed SQL, fresh subnet, disposable advisory connection")
    finally:
        reader.dispose()
        db.close()


with tempfile.TemporaryDirectory(prefix="um-child-readonly-") as directory:
    path = Path(directory) / "readonly ? # test.db"
    engine = create_engine("sqlite:///" + str(path))
    try:
        scenario(engine)
    finally:
        engine.dispose()
    missing = Path(directory) / "missing.db"
    refuses(lambda: child.ChildDatabase("sqlite:///" + str(missing)))
    assert not missing.exists()
    for url in ("sqlite://", "sqlite:///:memory:", "sqlite:///" + str(path) + "?mode=rw",
                "postgresql://localhost/unused", "mysql+pymysql://localhost/test?local_infile=1"):
        refuses(lambda url=url: child.ChildDatabase(url))
    maria = os.environ.get("MARIADB_TEST_URL", "").strip()
    if maria:
        engine = scratch.claim(maria)
        try:
            scenario(engine)
        finally:
            scratch.release(engine)
    elif os.environ.get("CI", "").lower() == "true":
        raise AssertionError("CI requires real MariaDB")
    else:
        print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
