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
import time


TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
# One script hanging is a FAILURE of that script, not a slow suite: the
# slowest take ~35 s in CI, so 10 minutes is far above any real run.
PER_TEST_TIMEOUT = int(os.environ.get("UM_TEST_TIMEOUT", "600"))


def main() -> int:
    tests = sorted(TEST_DIR.glob("test_*.py"))
    failures: list[tuple[Path, str]] = []

    env = os.environ.copy()
    # Never let a developer's configured production database leak into a
    # test process. Each database-using script selects its own isolated
    # SQLite URL; removing the inherited value lets that choice take effect.
    env.pop("DATABASE_URL", None)

    durations: list[tuple[float, str]] = []
    for test in tests:
        started = time.monotonic()
        try:
            result = subprocess.run(
                [sys.executable, str(test)],
                cwd=BACKEND_DIR,
                env=env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=PER_TEST_TIMEOUT,
            )
            code, output = result.returncode, result.stdout
        except subprocess.TimeoutExpired as exc:
            partial = exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            code, output = "timeout", f"{partial}\nTIMED OUT after {PER_TEST_TIMEOUT}s"
        took = time.monotonic() - started
        durations.append((took, test.name))
        # flush: CI shows each line when it happens, so a slow test is
        # visible by its own timestamp instead of all lines at the end.
        if code == 0:
            print(f"PASS  {test.name}  ({took:.1f}s)", flush=True)
        else:
            print(f"FAIL  {test.name}  ({took:.1f}s){'  TIMED OUT' if code == 'timeout' else ''}", flush=True)
            failures.append((test, output))

    print("\n" + "=" * 72)
    print(f"{len(tests) - len(failures)} passed, {len(failures)} failed, {len(tests)} total")
    print("slowest: " + ", ".join(f"{name} {took:.0f}s" for took, name in sorted(durations, reverse=True)[:10]))
    for test, output in failures:
        print(f"\n--- {test.name} output ---\n{output.rstrip()}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
