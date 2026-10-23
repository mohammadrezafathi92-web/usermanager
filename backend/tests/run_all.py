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
import signal
import subprocess
import sys
import tempfile
import time


TEST_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TEST_DIR.parent
# One script hanging is a FAILURE of that script, not a slow suite: the
# slowest take ~35 s in CI, so 10 minutes is far above any real run.
PER_TEST_TIMEOUT = int(os.environ.get("UM_TEST_TIMEOUT", "600"))
TERMINATE_GRACE_SECONDS = 1


def _stop_test_process(process: subprocess.Popen) -> None:
    """Stop only the isolated test session, then reap its direct child.

    POSIX descendants inherit the test's fresh process group; killing only
    the Python script leaves its runner/server children and their locks alive.
    A deliberately detached descendant is outside that group. On Windows
    this is direct-child cleanup only; POSIX group guarantees are not claimed.
    """
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # Do not reap the group leader during the grace period: keeping its
        # PID allocated avoids a reap-then-killpg PID/PGID reuse window.
        time.sleep(TERMINATE_GRACE_SECONDS)
        # Even if the group leader exited on TERM, descendants may ignore it.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=TERMINATE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
    process.wait(timeout=TERMINATE_GRACE_SECONDS)


def run_test(test: Path, env: dict) -> tuple[int | str, str]:
    # A temporary output file avoids an unbounded communicate() waiting for
    # inherited pipe handles after a timeout. It also retains partial output.
    with tempfile.TemporaryFile() as captured:
        process = subprocess.Popen(
            [sys.executable, str(test)], cwd=BACKEND_DIR, env=env,
            stdout=captured, stderr=subprocess.STDOUT,
            start_new_session=os.name == "posix",
        )
        completed = False
        try:
            try:
                code = process.wait(timeout=PER_TEST_TIMEOUT)
            except subprocess.TimeoutExpired:
                _stop_test_process(process)
                code = "timeout"
            completed = True
        finally:
            # Interruptions propagate; their test children do not keep running.
            if not completed:
                original_error = sys.exc_info()[1]
                try:
                    _stop_test_process(process)
                except (OSError, subprocess.TimeoutExpired) as cleanup_error:
                    if original_error is None:
                        raise
                    message = f"Test-process cleanup also failed ({type(cleanup_error).__name__})"
                    if hasattr(original_error, "add_note"):
                        original_error.add_note(message)
                    else:
                        print(message, file=sys.stderr)
        captured.seek(0)
        output = captured.read().decode("utf-8", errors="replace")
    if code == "timeout":
        output += f"\nTIMED OUT after {PER_TEST_TIMEOUT}s"
    return code, output


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
        code, output = run_test(test, env)
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
