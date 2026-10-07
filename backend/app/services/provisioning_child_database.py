"""Closed, read-only database access for the future runner child (10.9).

No application config, ORM, models, startup wiring or remote action import.
The parent supplies its trusted configured URL, never a customer's DTO field.
Each connection is disposable (NullPool): closing it also releases MariaDB
advisory locks. SQL is exact-allowlisted, not accepted merely for starting
with SELECT. This protects against programming mistakes, not malicious Python
code accessing private attributes or replacing the driver.
"""
import sqlite3
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.sql.elements import TextClause

from .provisioning_dispatch_binding import dispatch_statement
from .provisioning_contract_rules import contract_statement

READ_RUNTIME = ("SELECT installation_uuid, owner_state, owner_host_id, owner_boot_id, "
    "ownership_epoch, lock_backend, gate_mode, gate_mode_epoch "
    "FROM provisioning_runtime_state WHERE id = 1")
READ_INSTALLATION = "SELECT installation_uuid FROM provisioning_runtime_state WHERE id = 1"
READ_SUBNET = "SELECT mt_client_subnet FROM nodes WHERE id=:node_id"
ADVISORY_GET = "SELECT GET_LOCK(:name,:timeout)"
ADVISORY_CHECK = "SELECT IS_USED_LOCK(:name)=CONNECTION_ID()"
ADVISORY_RELEASE = "SELECT RELEASE_LOCK(:name)"


class ChildDatabaseUnavailable(RuntimeError):
    pass


def _sqlite_connection(path):
    connection = sqlite3.connect("file:" + quote(str(path), safe="/") + "?mode=ro", uri=True, timeout=15)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone() != (1,):
            raise ChildDatabaseUnavailable("child_database_not_readonly")
        writes = frozenset(getattr(sqlite3, name) for name in (
            "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX", "SQLITE_CREATE_TABLE",
            "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE", "SQLITE_CREATE_TEMP_TRIGGER",
            "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER", "SQLITE_CREATE_VIEW", "SQLITE_DROP_INDEX",
            "SQLITE_DROP_TABLE", "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
            "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_ALTER_TABLE",
            "SQLITE_ATTACH", "SQLITE_DETACH", "SQLITE_REINDEX", "SQLITE_ANALYZE"))
        def authorize(action, first, second, database, source):
            if action in writes or (action == sqlite3.SQLITE_PRAGMA and (
                    first not in ("query_only", "read_uncommitted") or second is not None)) or (
                    action == sqlite3.SQLITE_FUNCTION and second in ("load_extension", "writefile")):
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return connection
    except Exception:
        connection.close()
        raise


class _ReadConnection:
    """Only execute/close/context management; no raw_connection or commit API."""
    def __init__(self, owner):
        self._owner = owner
        try:
            self._connection = owner._engine.connect()
        except Exception:
            raise ChildDatabaseUnavailable("child_database_connection_unavailable") from None

    def execute(self, statement, parameters=None):
        if type(statement) is not TextClause or str(statement) not in self._owner._allowed:
            raise ChildDatabaseUnavailable("child_database_query_not_allowed")
        if self._connection.closed or self._connection.invalidated:
            raise ChildDatabaseUnavailable("child_database_connection_lost")
        return self._connection.execute(statement, parameters or {})

    def end_snapshot(self):
        """End only a read transaction; keep this dedicated physical connection."""
        if self._connection.closed or self._connection.invalidated:
            raise ChildDatabaseUnavailable("child_database_connection_lost")
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.close()


class ChildDatabase:
    def __init__(self, trusted_url):
        self._engine = None
        try:
            url = make_url(trusted_url)
            if url.drivername in ("sqlite", "sqlite+pysqlite") and not url.query:
                if not url.database or url.database == ":memory:":
                    raise ValueError()
                path = Path(url.database).resolve(strict=True)
                if not path.is_file():
                    raise ValueError()
                self._engine = create_engine("sqlite+pysqlite://", poolclass=NullPool,
                    creator=lambda: _sqlite_connection(path), hide_parameters=True)
            elif url.drivername in ("mysql+pymysql", "mariadb+pymysql") and url.database and dict(url.query) in (
                    {}, {"charset": "utf8mb4"}):
                self._engine = create_engine(url.set(query={}), poolclass=NullPool, hide_parameters=True,
                    connect_args=dict(charset="utf8mb4", connect_timeout=10, read_timeout=15, write_timeout=15))
                @event.listens_for(self._engine, "connect")
                def read_only(connection, record):
                    cursor = connection.cursor()
                    try:
                        cursor.execute("SET SESSION TRANSACTION READ ONLY")
                        cursor.execute("SELECT @@session.tx_read_only")
                        if cursor.fetchone() != (1,):
                            raise ChildDatabaseUnavailable("child_database_not_readonly")
                    finally:
                        cursor.close()
            else:
                raise ValueError()
            # Initialize SQLAlchemy's trusted dialect metadata queries before
            # publishing the closed connection API. No application SQL runs
            # here; the driver is already read-only on this connection.
            with self._engine.connect():
                if self.dialect.name in ("mysql", "mariadb") and not self.dialect.is_mariadb:
                    raise ValueError()
        except Exception:
            if self._engine is not None:
                self._engine.dispose()
            raise ChildDatabaseUnavailable("child_database_configuration_invalid") from None
        self._allowed = {READ_RUNTIME, READ_INSTALLATION, READ_SUBNET,
            str(dispatch_statement(self.dialect.name)), str(contract_statement())}
        if self.dialect.name in ("mysql", "mariadb"):
            self._allowed.update((ADVISORY_GET, ADVISORY_CHECK, ADVISORY_RELEASE))
        # Also deny through accidental private engine/exec_driver_sql use.
        @event.listens_for(self._engine, "before_cursor_execute")
        def closed_sql(connection, cursor, statement, parameters, context, executemany):
            allowed = {str(text(value).compile(dialect=self.dialect)) for value in self._allowed}
            if statement not in allowed or executemany:
                raise ChildDatabaseUnavailable("child_database_query_not_allowed")

    @property
    def dialect(self):
        return self._engine.dialect

    def connect(self):
        return _ReadConnection(self)

    def dispose(self):
        self._engine.dispose()
