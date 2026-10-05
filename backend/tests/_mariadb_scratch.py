"""The ONE way a test may use the server behind MARIADB_TEST_URL.

Tests need a whole empty schema and want to throw it away afterwards. The
only way to do that without ever being able to hurt existing data is not
to touch existing databases at all:

    claim(url)   connects to the server the URL names and CREATEs a brand
                 new database with a random name, um_test_run_<12 hex>.
                 The engine it returns is bound to that new database only.
    wipe(engine) drops the tables inside that new database.
    release(e)   DROPs that new database.

Nothing here ever creates, alters or drops a table in a database that
already existed - including the one named in the URL, which is used only
as a place to connect to. wipe() and release() act solely on a database
this very process created (remembered by name and by engine), and the
name must still have the run-database form. There is no marker and no
allowlist to get wrong, and no name, host or port to compare: a database
with real data is, by construction, never one we just created.

The account in the URL needs the right to create and drop databases; if it
cannot, claim() raises ScratchRefused and the caller reports a failure.
"""
from __future__ import annotations

import re
import secrets

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url

RUN_PREFIX = "um_test_run_"
_RUN_NAME = re.compile(r"^um_test_run_[0-9a-f]{12}$")
# The engines claim() handed out, with the database created for each. The
# engine OBJECT is kept (not its id(), which Python reuses once an object is
# gone), so "is this ours?" is an identity check against a live object.
_created: list[tuple[Engine, str]] = []


class ScratchRefused(RuntimeError):
    """Not a database this process created - nothing was touched."""


def _own_database(engine: Engine) -> str:
    """The name of the run database behind this engine, or a refusal."""
    name = next((created for owner, created in _created if owner is engine), None)
    if name is None or not _RUN_NAME.match(name) or engine.url.database != name:
        raise ScratchRefused("this engine is not on a database created by scratch.claim() in this process")
    return name


def _drop_database(server_url, name: str) -> None:
    if not _RUN_NAME.match(name):
        raise ScratchRefused(f"'{name}' is not a run database name")
    server = create_engine(server_url)
    try:
        with server.connect() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS `{name}`")
    finally:
        server.dispose()


def claim(url: str) -> Engine:
    """Creates a new, empty, randomly named database on the server and
    returns an engine bound to it. Existing databases are not modified."""
    parsed = make_url(url)
    if parsed.get_backend_name() not in ("mysql", "mariadb"):
        raise ScratchRefused("MARIADB_TEST_URL must be a MySQL/MariaDB URL")
    name = RUN_PREFIX + secrets.token_hex(6)
    server = create_engine(parsed)
    try:
        with server.connect() as conn:
            # CREATE (never "IF NOT EXISTS"): if the name somehow exists, fail rather than adopt it.
            conn.exec_driver_sql(f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4")
    except Exception as exc:  # noqa: BLE001
        raise ScratchRefused(f"could not create a run database on the test server ({type(exc).__name__}) - the account "
                             f"in MARIADB_TEST_URL needs CREATE/DROP on {RUN_PREFIX}*") from exc
    finally:
        server.dispose()
    try:
        engine = create_engine(parsed.set(database=name))
    except Exception:
        _drop_database(parsed, name)        # we created it a moment ago: do not leave it behind
        raise
    _created.append((engine, name))
    return engine


def wipe(engine: Engine) -> None:
    """Drops every table in the run database (and only there)."""
    name = _own_database(engine)
    with engine.begin() as conn:
        if conn.exec_driver_sql("SELECT DATABASE()").scalar() != name:
            raise ScratchRefused("connected to another database than the one this process created")
        conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")     # the app's schema has a foreign-key cycle
        try:
            for table in inspect(conn).get_table_names(schema=name):
                conn.exec_driver_sql(f"DROP TABLE IF EXISTS `{name}`.`{table.replace('`', '``')}`")
        finally:
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def release(engine: Engine) -> None:
    """Drops the run database itself. A no-op for anything else. The
    engine is forgotten only once its database is really gone: if the drop
    fails the error is raised and release() can be called again."""
    try:
        name = _own_database(engine)
    except ScratchRefused:
        engine.dispose()
        return
    engine.dispose()
    _drop_database(engine.url.set(database=None), name)
    _created[:] = [(owner, created) for owner, created in _created if owner is not engine]
