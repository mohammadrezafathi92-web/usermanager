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
check("the guard has exactly two destructive statements, and both name the run database it created",
      (source.count("DROP DATABASE IF EXISTS `{name}`"), source.count("DROP TABLE IF EXISTS `{name}`.`"),
       source.count('exec_driver_sql(f"DROP'), source.count("TRUNCATE"), source.count("DELETE FROM")), (1, 1, 2, 0, 0))
own_tree = ast.parse(open(os.path.abspath(__file__), encoding="utf-8").read())


def _sql_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value for part in node.values if isinstance(part, ast.Constant))
    return "?"


observer_statements, observer_transactions = [], 0
for node in ast.walk(own_tree):
    if isinstance(node, ast.With):
        for item in node.items:
            call = item.context_expr
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name) and call.func.value.id == "observer"):
                if call.func.attr != "connect":
                    observer_transactions += 1
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute) and inner.func.attr == "exec_driver_sql":
                        observer_statements.append(_sql_text(inner.args[0]).split()[0].upper())
check("this test only READS the URL's database: every statement on the observer connection is SELECT/SHOW/CHECKSUM, "
      "and it never opens a write transaction there",
      (sorted(set(observer_statements)), observer_transactions), (["CHECKSUM", "SELECT", "SHOW"], 0))
check("an engine object is tracked by identity, not by id() (which Python reuses)", isinstance(scratch._created, list), True)

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
    # The URL's own database is only OBSERVED here, never written to: no
    # witness table is planted in it (an earlier version did, and dropped a
    # fixed table name there - on a real database that is exactly the harm
    # this module exists to prevent).
    observer = create_engine(mariadb_url)

    def base_state():
        """What the URL's own database looks like: every table with its row count and checksum."""
        with observer.connect() as conn:
            state = {}
            for table in sorted(inspect(conn).get_table_names()):
                rows = conn.exec_driver_sql(f"SELECT COUNT(*) FROM `{table}`").scalar()
                checksum = conn.exec_driver_sql(f"CHECKSUM TABLE `{table}`").fetchone()[1]
                state[table] = (rows, checksum)
            return state

    def databases():
        with observer.connect() as conn:
            return {row[0] for row in conn.exec_driver_sql("SHOW DATABASES")}

    base_before, databases_before = base_state(), databases()
    try:
        engine = scratch.claim(mariadb_url)
        witness = scratch.claim(mariadb_url)            # stands for "somebody else's database" - also one WE created
        run_name, witness_name = engine.url.database, witness.url.database
        check("claim made NEW databases with run names, different from the URL's own and from each other",
              ([bool(scratch._RUN_NAME.match(n)) for n in (run_name, witness_name)],
               len({run_name, witness_name, observer.url.database}), {run_name, witness_name} <= databases()),
              ([True, True], 3, True))
        for target in (engine, witness):
            with target.begin() as conn:
                conn.exec_driver_sql("CREATE TABLE users (x INT)")
                conn.exec_driver_sql("INSERT INTO users VALUES (42)")
        scratch.wipe(engine)
        with engine.connect() as conn:
            emptied = inspect(conn).get_table_names()
        with witness.connect() as conn:
            kept = conn.exec_driver_sql("SELECT x FROM users").scalar()
        check("wipe empties its own run database; a table of the SAME NAME in another database keeps its row",
              (emptied, kept), ([], 42))
        impostor = create_engine(engine.url)            # same database, but not the engine claim() returned
        check("an engine not handed out by claim() is refused even on a run database", refused(scratch.wipe, impostor), True)
        impostor.dispose()
        scratch.release(engine)
        check("release drops exactly its own database", (run_name in databases(), witness_name in databases()), (False, True))
        scratch.release(engine)                         # a second release of the same engine is a no-op
        scratch.release(witness)
        check("afterwards the server has exactly the databases it had before", databases(), databases_before)
        check("...and the URL's own database is byte-for-byte what it was: same tables, same row counts, same checksums",
              base_state(), base_before)
        check("nothing is remembered once released", scratch._created, [])
    finally:
        for leftover, _name in list(scratch._created):  # only databases this very process created
            scratch.release(leftover)
        observer.dispose()
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
