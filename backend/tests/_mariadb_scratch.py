"""The ONE way a test may touch the database behind MARIADB_TEST_URL.

Several tests create the whole schema there and drop every table when they
are done. Pointed at a real or shared database by mistake, that would
destroy it. So a test never drops anything on its own; it goes through
this module, which refuses unless ALL of this holds:

  1. the database NAME says it is disposable: one of its underscore-
     separated words is 'scratch' or 'test' (CI uses um_ci_scratch);
  2. the database is EMPTY, or already carries this module's marker table
     WITH this module's claim row in it, from an earlier test. A database
     with tables and no valid claim is never touched, whatever it is called;
  3. EVERY table in it is one these tests can create: a table of the
     application's own schema, the marker, or the guard's probe table. The
     marker alone proves nothing about the other tables - a run that died
     halfway leaves it behind, and someone may add a table later. One
     unknown table and the whole operation is refused: nothing is dropped;
  4. it is not a database a running panel is configured to use
     (DATABASE_URL).

claim() checks and puts the marker in; wipe() re-checks all of the above
and drops the known tables (everything except the marker); release() also
removes the marker.
"""
from __future__ import annotations

import os
import re

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url

MARKER = "um_test_scratch_marker"
CLAIM_TOKEN = "usermanager-backend-tests"
PROBE = "um_test_scratch_probe"            # the only non-application table a test may create


def known_tables() -> frozenset[str]:
    """Every table name these tests can create: the application's schema
    (all three metadatas), the marker and the probe."""
    from app import models, models_provisioning, models_receipt_void
    names = set(models.Base.metadata.tables)
    names.update(table.name for table in models_provisioning.PROVISIONING_TABLES)
    names.update(table.name for table in models_receipt_void.RECEIPT_VOID_TABLES)
    names.update((MARKER, PROBE))
    return frozenset(names)
_DISPOSABLE_WORD = re.compile(r"(^|_)(scratch|test)(_|$)", re.IGNORECASE)


class ScratchRefused(RuntimeError):
    """This database is not provably a throwaway one - nothing was touched."""


def _database_name(engine_or_url) -> str:
    url = engine_or_url.url if isinstance(engine_or_url, Engine) else make_url(engine_or_url)
    name = url.database or ""
    return os.path.splitext(os.path.basename(name))[0] if url.get_backend_name() == "sqlite" else name


def _check_name(engine_or_url) -> None:
    name = _database_name(engine_or_url)
    if not _DISPOSABLE_WORD.search(name):
        raise ScratchRefused(f"database '{name}' is not named as a scratch/test database")
    url = engine_or_url.url if isinstance(engine_or_url, Engine) else make_url(engine_or_url)
    live = os.environ.get("DATABASE_URL", "").strip()
    if live and url.get_backend_name() != "sqlite":
        try:
            live_url = make_url(live)
        except Exception:  # noqa: BLE001
            live_url = None
        if live_url is not None and (live_url.host, live_url.port, live_url.database) == (url.host, url.port, url.database):
            raise ScratchRefused("this is the database DATABASE_URL points at")


def _tables(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return inspect(conn).get_table_names()


def _verify(engine: Engine) -> list[str]:
    """The table names, after proving the database is ours to wipe: either
    empty, or validly claimed with nothing but known tables in it."""
    existing = _tables(engine)
    if not existing:
        return existing
    name = _database_name(engine)
    if MARKER not in existing:
        raise ScratchRefused(f"database '{name}' has {len(existing)} table(s) and no {MARKER} - not created by these tests")
    with engine.connect() as conn:
        try:
            claims = [row[0] for row in conn.exec_driver_sql(f"SELECT claimed_by FROM {MARKER}")]
        except Exception as exc:  # noqa: BLE001 - a marker of another shape is not our marker
            raise ScratchRefused(f"database '{name}': {MARKER} is not this guard's marker") from exc
    if claims != [CLAIM_TOKEN]:
        raise ScratchRefused(f"database '{name}': {MARKER} does not hold this guard's claim")
    unknown = sorted(set(existing) - known_tables())
    if unknown:
        raise ScratchRefused(f"database '{name}' contains table(s) these tests never create: {', '.join(unknown[:5])}"
                             f"{' ...' if len(unknown) > 5 else ''} - refusing to drop anything")
    return existing


def claim(url: str) -> Engine:
    """An engine on a database that is provably disposable, with the marker
    in place. Raises ScratchRefused (and changes nothing) otherwise."""
    _check_name(url)
    engine = create_engine(url)
    try:
        if not _verify(engine):
            with engine.begin() as conn:
                conn.exec_driver_sql(f"CREATE TABLE {MARKER} (claimed_by VARCHAR(64) NOT NULL)")
                conn.exec_driver_sql(f"INSERT INTO {MARKER} (claimed_by) VALUES ('{CLAIM_TOKEN}')")
        return engine
    except Exception:
        engine.dispose()
        raise


def _drop(engine: Engine, names: list[str]) -> None:
    mysql = engine.dialect.name in ("mysql", "mariadb")
    quote = "`" if mysql else '"'
    with engine.begin() as conn:
        if mysql:
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 0")     # the app's schema has a foreign-key cycle
        for name in names:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {quote}{name}{quote}")
        if mysql:
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")


def wipe(engine: Engine) -> None:
    """Drops every table except the marker - after checking, again, that
    the database is validly claimed and holds nothing but known tables."""
    _check_name(engine)
    existing = _verify(engine)
    if MARKER not in existing:
        raise ScratchRefused(f"database '{_database_name(engine)}' was not claimed by these tests - nothing dropped")
    _drop(engine, [name for name in existing if name != MARKER])


def release(engine: Engine) -> None:
    """wipe(), then the marker too, then dispose. Never raises: a refusal
    here means there is nothing of ours to clean."""
    try:
        wipe(engine)
        _drop(engine, [MARKER])
    except ScratchRefused:
        pass
    finally:
        engine.dispose()
