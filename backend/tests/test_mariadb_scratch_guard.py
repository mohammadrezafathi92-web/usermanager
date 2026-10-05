"""tests/_mariadb_scratch.py refuses to touch a database that is not
provably a throwaway one - so a wrong MARIADB_TEST_URL cannot wipe real data.

Run:  python3 backend/tests/test_mariadb_scratch_guard.py

The guard is exercised here on SQLite files (same code path; only the
quoting and the FOREIGN_KEY_CHECKS statements differ) and, when
MARIADB_TEST_URL is set, on the real scratch database.
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
folder = tempfile.mkdtemp()


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def url(name):
    return f"sqlite:///{folder}/{name}.db"


def tables(name):
    engine = create_engine(url(name))
    try:
        return sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def make(name, *names):
    engine = create_engine(url(name))
    with engine.begin() as conn:
        for table in names:
            conn.exec_driver_sql(f"CREATE TABLE {table} (x INTEGER)")
    engine.dispose()


def refused(fn, *args):
    try:
        fn(*args)
    except scratch.ScratchRefused:
        return True
    return False


print("--- the name ---")
check("names that do not say 'scratch' or 'test' are refused",
      [refused(scratch._check_name, f"mysql+pymysql://u:p@h/{n}") for n in
       ("usermanager", "panel", "production", "latest", "contest", "testing_prod_copy", "")], [True] * 7)
check("names with the word are accepted",
      [refused(scratch._check_name, f"mysql+pymysql://u:p@h/{n}") for n in ("um_ci_scratch", "um_test", "test", "scratch_1")],
      [False] * 4)
os.environ["DATABASE_URL"] = "mysql+pymysql://u:p@db:3306/um_test"
check("never the database the panel itself is configured to use, even if it is called a test database",
      (refused(scratch._check_name, "mysql+pymysql://x:y@db:3306/um_test"),
       refused(scratch._check_name, "mysql+pymysql://x:y@other:3306/um_test")), (True, False))
os.environ["DATABASE_URL"] = "sqlite://"

print("--- a database with someone else's tables ---")
make("prod", "users", "ledger_entries")
check("wrong name: refused, tables untouched", (refused(scratch.claim, url("prod")), tables("prod")), (True, ["ledger_entries", "users"]))
make("um_test_shared", "users", "ledger_entries")
check("right name but NOT empty and no marker: refused, tables untouched",
      (refused(scratch.claim, url("um_test_shared")), tables("um_test_shared")), (True, ["ledger_entries", "users"]))
engine = create_engine(url("um_test_shared"))
check("wipe on an unclaimed database drops nothing", (refused(scratch.wipe, engine), tables("um_test_shared")),
      (True, ["ledger_entries", "users"]))
scratch.release(engine)
check("release on an unclaimed database drops nothing either", tables("um_test_shared"), ["ledger_entries", "users"])

print("--- an empty scratch database ---")
engine = scratch.claim(url("um_ci_scratch"))
check("claimed: the marker is in", tables("um_ci_scratch"), [scratch.MARKER])
with engine.begin() as conn:
    conn.exec_driver_sql("CREATE TABLE users (x INTEGER)")
    conn.exec_driver_sql("CREATE TABLE nodes (x INTEGER)")
scratch.wipe(engine)
check("wipe drops what the tests made and keeps the claim", tables("um_ci_scratch"), [scratch.MARKER])
engine.dispose()
again = scratch.claim(url("um_ci_scratch"))
check("a later test of the same run may claim it again", tables("um_ci_scratch"), [scratch.MARKER])
scratch.release(again)
check("release leaves it empty", tables("um_ci_scratch"), [])

print("--- no test drops tables on MARIADB_TEST_URL by itself ---")
tests_dir = os.path.dirname(os.path.abspath(__file__))
offenders = []
for name in sorted(os.listdir(tests_dir)):
    if not name.startswith("test_") or not name.endswith(".py") or name == os.path.basename(__file__):
        continue
    source = open(os.path.join(tests_dir, name), encoding="utf-8").read()
    if "MARIADB_TEST_URL" not in source:
        continue
    tree = ast.parse(source)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    direct = [n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "create_engine"
              and n.args and isinstance(n.args[0], ast.Name) and n.args[0].id == "mariadb_url"]
    if direct or "FOREIGN_KEY_CHECKS" in source or "scratch.claim(" not in source:
        offenders.append((name, direct))
check("every test that uses MARIADB_TEST_URL goes through scratch.claim and has no drop-everything of its own",
      offenders, [])

print("--- the real scratch database ---")
mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    try:
        maria = scratch.claim(mariadb_url)
        claimed = True
    except scratch.ScratchRefused as exc:
        claimed, maria = str(exc), None
    check("CI's database is accepted as a scratch database", claimed, True)
    if maria is not None:
        with maria.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE guard_probe (x INT)")
        scratch.wipe(maria)
        with maria.connect() as conn:
            left = sorted(inspect(conn).get_table_names())
        check("MariaDB: wipe keeps only the marker", left, [scratch.MARKER])
        scratch.release(maria)
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
