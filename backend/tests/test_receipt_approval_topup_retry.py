"""A2: which database errors make a wallet top-up start over, and how often.

The whole top-up transaction is retried on a lock conflict - MariaDB 1020
(row changed since the snapshot), 1205 (lock wait timeout), 1213 (deadlock),
SQLite "database is locked"/"busy" - and on nothing else. This drives the
retry wrapper with made-up driver errors, so it decides the same way with
no MariaDB at hand; the real lock order is exercised by the acceptance and
concurrency tests on a real MariaDB in CI.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app.services import receipt_approval_topup as topup

failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class DriverError(Exception):
    """Shaped like pymysql.err.OperationalError: args = (code, message)."""


def mysql_error(code):
    return OperationalError("UPDATE users", {}, DriverError(code, "made up"))


def sqlite_error(message):
    return OperationalError("UPDATE users", {}, sqlite3.OperationalError(message))


class FakeSession:
    def __init__(self, dialect):
        self.dialect, self.rollbacks = dialect, 0

    def get_bind(self):
        return types.SimpleNamespace(dialect=types.SimpleNamespace(name=self.dialect))

    def rollback(self):
        self.rollbacks += 1


for code, expected in ((1020, True), (1205, True), (1213, True), (1045, False), (2006, False), (1062, False)):
    check(f"MariaDB error {code} restarts the top-up: {expected}", topup._transient(mysql_error(code), "mysql"), expected)
for message, expected in (("database is locked", True), ("database table is busy", True), ("no such table: users", False)):
    check(f"SQLite '{message}' restarts the top-up: {expected}", topup._transient(sqlite_error(message), "sqlite"), expected)
check("a MariaDB code is not read as a SQLite conflict", topup._transient(mysql_error(1213), "sqlite"), False)


def run(dialect, outcomes):
    """Drive apply() with a scripted _run_once; return (result, calls, rollbacks)."""
    script, calls, session = list(outcomes), [0], FakeSession(dialect)

    def scripted(*_args):
        calls[0] += 1
        outcome = script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    real_run, real_sleep = topup._run_once, topup.time.sleep
    topup._run_once, topup.time.sleep = scripted, lambda _seconds: None
    try:
        try:
            result = topup.apply(session, None, "u", 1, None, None, lambda _user: None)
        except HTTPException as refused:
            result = (refused.status_code, refused.detail)
        except OperationalError:
            result = "raised OperationalError"
        return result, calls[0], session.rollbacks
    finally:
        topup._run_once, topup.time.sleep = real_run, real_sleep


check("no conflict: one attempt, no rollback", run("mysql", ["ok"]), ("ok", 1, 0))
check("a deadlock then success: second attempt answers, after a rollback",
      run("mysql", [mysql_error(1213), "ok"]), ("ok", 2, 1))
check("1020 then 1205 then success: third attempt answers",
      run("mysql", [mysql_error(1020), mysql_error(1205), "ok"]), ("ok", 3, 2))
check(f"{topup.ATTEMPTS} conflicts in a row: 503 database_busy, every attempt rolled back",
      run("mysql", [mysql_error(1213)] * topup.ATTEMPTS), ((503, "database_busy"), topup.ATTEMPTS, topup.ATTEMPTS))
check("an error that is not a lock conflict is not retried",
      run("mysql", [mysql_error(1045), "ok"]), ("raised OperationalError", 1, 1))
check("SQLite 'database is locked' then success: second attempt answers",
      run("sqlite", [sqlite_error("database is locked"), "ok"]), ("ok", 2, 1))
check("a refusal is passed on as it is, rolled back, never retried",
      run("mysql", [HTTPException(status_code=409, detail="approval_has_effects"), "ok"]),
      ((409, "approval_has_effects"), 1, 1))

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
