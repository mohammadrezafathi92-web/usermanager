"""tests/_mariadb_scratch.py only ever works inside a database it created
itself - so a wrong MARIADB_TEST_URL cannot hurt existing data.

Run:  python3 backend/tests/test_mariadb_scratch_guard.py

Without a MariaDB server only the refusals can be checked. With
MARIADB_TEST_URL (mandatory in CI) the real behaviour is checked: a table
that already existed in the URL's own database is still there, with its
row, after claim -> wipe -> release, and the run database is gone.
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, inspect

import _mariadb_scratch as scratch

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def refused(fn, *args):
    try:
        fn(*args)
    except scratch.ScratchRefused:
        return True
    return False


print("--- only a database this process created ---")
foreign_path = os.path.join(tempfile.mkdtemp(), "um_test_run_0123456789ab.db")     # even with a run-database NAME
foreign = create_engine(f"sqlite:///{foreign_path}")
with foreign.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE users (x INTEGER)")
    conn.exec_driver_sql("INSERT INTO users VALUES (1)")
check("wipe refuses an engine that did not come from claim()", refused(scratch.wipe, foreign), True)
scratch.release(foreign)
again = create_engine(f"sqlite:///{foreign_path}")
with again.connect() as conn:
    left = (sorted(inspect(conn).get_table_names()), conn.exec_driver_sql("SELECT COUNT(*) FROM users").scalar())
again.dispose()
check("release on such an engine drops nothing", left, (["users"], 1))
check("claim only accepts a MySQL/MariaDB URL", refused(scratch.claim, f"sqlite:///{foreign_path}"), True)
check("a server that cannot be reached / will not create a database is a refusal, not a crash",
      refused(scratch.claim, "mysql+pymysql://nobody:nothing@127.0.0.1:1/x"), True)
check("the run-database name form", [bool(scratch._RUN_NAME.match(n)) for n in
                                      ("um_test_run_0123456789ab", "um_test_run_", "um_test_run_0123456789abX", "usermanager",
                                       "um_test", "um_test_run_0123456789AB")], [True, False, False, False, False, False])
source = open(scratch.__file__, encoding="utf-8").read()
check("the guard has no statement that could reach an existing database: every DROP names the run database",
      (source.count("DROP DATABASE IF EXISTS `{name}`"), source.count("DROP TABLE IF EXISTS `{name}`.`{table}`"),
       source.count('exec_driver_sql(f"DROP')), (1, 1, 2))

print("--- no test opens MARIADB_TEST_URL by itself ---")
tests_dir = os.path.dirname(os.path.abspath(__file__))
offenders = []
for name in sorted(os.listdir(tests_dir)):
    if not name.startswith("test_") or not name.endswith(".py") or name == os.path.basename(__file__):
        continue
    text = open(os.path.join(tests_dir, name), encoding="utf-8").read()
    if "MARIADB_TEST_URL" not in text:
        continue
    direct = [n.lineno for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
              and n.func.id == "create_engine" and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "mariadb_url"]
    if direct or "FOREIGN_KEY_CHECKS" in text or "DROP DATABASE" in text or "scratch.claim(" not in text:
        offenders.append((name, direct))
check("every test that uses MARIADB_TEST_URL goes through scratch.claim and has no drop of its own", offenders, [])

print("--- on the real server ---")
mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    PRE = "um_guard_preexisting"                    # stands for data that was there before the tests
    base = create_engine(mariadb_url)               # the URL's own database - the one that must stay untouched
    with base.begin() as conn:
        conn.exec_driver_sql(f"DROP TABLE IF EXISTS {PRE}")
        conn.exec_driver_sql(f"CREATE TABLE {PRE} (x INT)")
        conn.exec_driver_sql(f"INSERT INTO {PRE} VALUES (42)")

    def preexisting():
        with base.connect() as conn:
            return conn.exec_driver_sql(f"SELECT x FROM {PRE}").scalar()

    def databases():
        with base.connect() as conn:
            return {row[0] for row in conn.exec_driver_sql("SHOW DATABASES")}

    try:
        engine = scratch.claim(mariadb_url)
        run_name = engine.url.database
        check("claim made a NEW database with a run name, different from the URL's own",
              (bool(scratch._RUN_NAME.match(run_name)), run_name != base.url.database, run_name in databases()), (True, True, True))
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE users (x INT)")
            conn.exec_driver_sql(f"CREATE TABLE {PRE} (x INT)")     # same NAME as the pre-existing table, in OUR database
        scratch.wipe(engine)
        with engine.connect() as conn:
            emptied = inspect(conn).get_table_names()
        check("wipe empties the run database; the table of the same name in the URL's database keeps its row",
              (emptied, preexisting()), ([], 42))
        impostor = create_engine(engine.url)        # same database, but not the engine claim() returned
        check("an engine not handed out by claim() is refused even on the run database", refused(scratch.wipe, impostor), True)
        impostor.dispose()
        second = scratch.claim(mariadb_url)
        check("each claim is its own database", second.url.database != run_name, True)
        scratch.release(second)
        scratch.release(engine)
        check("release dropped both run databases and nothing else: the pre-existing table still has its row",
              (run_name in databases(), second.url.database in databases(), preexisting()), (False, False, 42))
    finally:
        with base.begin() as conn:
            conn.exec_driver_sql(f"DROP TABLE IF EXISTS {PRE}")     # the one table this test itself put there
        base.dispose()
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
