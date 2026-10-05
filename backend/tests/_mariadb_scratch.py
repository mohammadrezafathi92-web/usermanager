"""The ONE way a test may touch the database behind MARIADB_TEST_URL.

Several tests create the whole schema there and drop every table when they
are done. Pointed at a real or shared database by mistake, that would
destroy it. So a test never drops anything on its own; it goes through
this module, which refuses unless ALL of this holds:

  1. the database NAME says it is disposable: one of its underscore-
     separated words is 'scratch' or 'test' (CI uses um_ci_scratch);
  2. the database is EMPTY, or already carries this module's marker table
     from an earlier test of the same run - i.e. every table in it was
     created by these tests. A database with other tables and no marker is
     never touched, whatever it is called;
  3. it is not a database a running panel is configured to use
     (DATABASE_URL).

claim() checks and puts the marker in; wipe() drops the tables these tests
made (everything except the marker - by rule 2 nothing else can be there);
release() also removes the marker.
"""
from __future__ import annotations

import os
import re

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine, make_url

MARKER = "um_test_scratch_marker"
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


def claim(url: str) -> Engine:
    """An engine on a database that is provably disposable, with the marker
    in place. Raises ScratchRefused (and changes nothing) otherwise."""
    _check_name(url)
    engine = create_engine(url)
    try:
        existing = _tables(engine)
        if existing and MARKER not in existing:
            raise ScratchRefused(f"database '{_database_name(engine)}' already has {len(existing)} table(s) that these "
                                 f"tests did not create (no {MARKER}) - refusing to use it")
        if MARKER not in existing:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"CREATE TABLE {MARKER} (claimed_by VARCHAR(64) NOT NULL)")
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
    """Drops every table except the marker. Only on a claimed database."""
    _check_name(engine)
    existing = _tables(engine)
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
