"""Run the repository's standalone backend checks in isolated processes.

The historical tests are executable scripts rather than pytest functions;
many intentionally call ``sys.exit(1)`` on failure. Importing them from
pytest therefore aborts collection and also leaks mutated module globals
between files. This runner preserves their existing contract while giving
local development and CI one deterministic command.

Run from the repository root with::

    backend/venv/bin/python backend/tests/run_all.py
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent


def main() -> int:
    tests = sorted(TEST_DIR.glob("test_*.py"))
    failures: list[tuple[Path, str]] = []

    env = os.environ.copy()
    # Never let a developer's configured production database leak into a
    # test process. Each database-using script selects its own isolated
    # SQLite URL; removing the inherited value lets that choice take effect.
    env.pop("DATABASE_URL", None)

    for test in tests:
        result = subprocess.run(
            [sys.executable, str(test)],
            cwd=BACKEND_DIR,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        if result.returncode == 0:
            print(f"PASS  {test.name}")
        else:
            print(f"FAIL  {test.name}")
            failures.append((test, result.stdout))

    print("\n" + "=" * 72)
    print(f"{len(tests) - len(failures)} passed, {len(failures)} failed, {len(tests)} total")
    for test, output in failures:
        print(f"\n--- {test.name} output ---\n{output.rstrip()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
