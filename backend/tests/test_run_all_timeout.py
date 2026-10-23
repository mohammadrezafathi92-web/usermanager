"""Runner timeouts must stop subprocess descendants, retain output and fail.

Runs only tiny temporary fixture scripts, never this repository's suite.
POSIX process groups are verified with actual child processes; Windows
retains the direct-child compatibility path and skips the POSIX-only case.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("isolated_test_runner", Path(__file__).with_name("run_all.py"))
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def lock_released(path):
    import fcntl
    with path.open("a+b") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False


def run_fixture(folder, timeout):
    previous = runner.TEST_DIR, runner.BACKEND_DIR, runner.PER_TEST_TIMEOUT
    runner.TEST_DIR = runner.BACKEND_DIR = folder
    runner.PER_TEST_TIMEOUT = timeout
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            status = runner.main()
        return status, output.getvalue()
    finally:
        runner.TEST_DIR, runner.BACKEND_DIR, runner.PER_TEST_TIMEOUT = previous


with tempfile.TemporaryDirectory(prefix="runner-timeout-regression-") as temporary:
    folder = Path(temporary)
    (folder / "test_failure.py").write_text("print('failure-output', flush=True)\nraise SystemExit(7)\n")
    status, output = run_fixture(folder, 5)
    assert status == 1 and "FAIL  test_failure.py" in output and "failure-output" in output, output
    if hasattr(runner, "run_test"):
        code, _ = runner.run_test(folder / "test_failure.py", os.environ.copy())
        assert code == 7, code
    print("PASS ordinary nonzero exit remains a failure with its output")
    (folder / "test_failure.py").unlink()
    (folder / "test_success.py").write_text("print('success-output')\n")
    status, output = run_fixture(folder, 5)
    assert status == 0 and "PASS  test_success.py" in output, output
    print("PASS ordinary successful script still passes")
    (folder / "test_success.py").unlink()

    (folder / "test_direct_timeout.py").write_text("import time\nprint('direct-partial',flush=True)\ntime.sleep(60)\n")
    status, output = run_fixture(folder, 1)
    assert status == 1 and "TIMED OUT" in output and "direct-partial" in output, output
    print("PASS ordinary TERM-responsive timeout remains a reported failure")
    (folder / "test_direct_timeout.py").unlink()

    if os.name == "posix":
        # The descendant ignores TERM, owns its inherited output handle and
        # would outlive a direct-child-only timeout. Wait for its ready file
        # before the parent announces its partial output.
        ready = folder / "descendant.pid"
        resource = folder / "descendant.lock"
        child_code = ("import fcntl,os,signal,time; from pathlib import Path; "
                      "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
                      f"lock=open({str(resource)!r},'a+b'); fcntl.flock(lock,fcntl.LOCK_EX); "
                      f"Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(60)")
        script = ("import signal,subprocess,sys,time\nfrom pathlib import Path\n"
                  "signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
                  f"subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
                  f"ready=Path({str(ready)!r})\n"
                  "deadline=time.monotonic()+5\n"
                  "while not ready.exists() and time.monotonic()<deadline: time.sleep(.01)\n"
                  "assert ready.exists(), 'descendant failed to start'\n"
                  "print('partial-before-timeout',flush=True)\ntime.sleep(60)\n")
        (folder / "test_a_hang.py").write_text(script)
        (folder / "test_b_after.py").write_text("print('next-script-ran')\n")
        pid = None
        try:
            started = time.monotonic()
            status, output = run_fixture(folder, 1)
            elapsed = time.monotonic() - started
            pid = int(ready.read_text())
            assert status == 1 and "FAIL  test_a_hang.py" in output and "TIMED OUT" in output, output
            assert "partial-before-timeout" in output and "PASS  test_b_after.py" in output, output
            assert elapsed < 8, elapsed
            deadline = time.monotonic() + 3
            while not lock_released(resource) and time.monotonic() < deadline:
                time.sleep(.02)
            assert lock_released(resource), f"descendant {pid} survived timeout cleanup and holds its lock"
            print("PASS timeout fails, preserves partial output and continues to the next script")
            print("PASS TERM-resistant descendant is stopped within a bounded interval")
            print("PASS isolated cleanup leaves this runner's own process group alive")
        finally:
            if pid is None and ready.exists():
                pid = int(ready.read_text())
            if pid is not None and pid != os.getpid() and not lock_released(resource):
                try:
                    os.kill(pid, signal.SIGKILL)  # exact PID of this fixture's child only
                except ProcessLookupError:
                    pass
    else:
        print("SKIP POSIX process-group cleanup check (native Windows not exercised)")

    if hasattr(runner, "run_test"):
        import subprocess
        original_popen = subprocess.Popen

        class InterruptOnce(original_popen):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self._interrupt_next_wait = True

            def wait(self, *args, **kwargs):
                if self._interrupt_next_wait:
                    self._interrupt_next_wait = False
                    raise KeyboardInterrupt()
                return super().wait(*args, **kwargs)

        interrupted = False
        interrupt_script = folder / "test_interrupt.py"
        interrupt_script.write_text("import time\ntime.sleep(5)\n")
        with patch.object(runner.subprocess, "Popen", InterruptOnce):
            try:
                runner.run_test(interrupt_script, os.environ.copy())
            except KeyboardInterrupt:
                interrupted = True
        assert interrupted, "runner swallowed KeyboardInterrupt"
        print("PASS KeyboardInterrupt propagates after isolated cleanup")

        original_stop = runner._stop_test_process

        def cleanup_error_after_stop(process):
            original_stop(process)
            raise subprocess.TimeoutExpired("injected bounded reap error", 1)

        interrupted = False
        with patch.object(runner.subprocess, "Popen", InterruptOnce), \
                patch.object(runner, "_stop_test_process", cleanup_error_after_stop):
            try:
                runner.run_test(interrupt_script, os.environ.copy())
            except KeyboardInterrupt:
                interrupted = True
        assert interrupted, "cleanup failure replaced KeyboardInterrupt"
        print("PASS bounded cleanup error does not mask the original interruption")

print("all runner timeout checks passed")
