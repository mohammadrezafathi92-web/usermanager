"""Cancellation barrier holds through action/commit, not just context creation."""
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import settings
from app.services.reseller_refund_fence import barrier, guarded, executor


with TemporaryDirectory() as directory, patch.dict(os.environ, {"UM_LOCK_DIR": directory}), \
     patch.object(settings, "reseller_cancellation_enabled", True):
    started, release, exclusive_started, exclusive_release = (threading.Event() for _ in range(4))
    @guarded
    def ordinary():
        started.set()
        assert release.wait(3)
    def cancellation():
        with barrier(exclusive=True):
            exclusive_started.set()
            assert exclusive_release.wait(3)
            # Nested remote actions must not try upgrading/reacquiring a lock.
            with barrier():
                pass
    with ThreadPoolExecutor(max_workers=3) as pool:
        writer = pool.submit(ordinary)
        assert started.wait(1)
        cancelling = pool.submit(cancellation)
        assert not exclusive_started.wait(.1)
        release.set(); writer.result(timeout=2)
        assert exclusive_started.wait(2)
        next_started = threading.Event()
        @guarded
        def next_writer():
            next_started.set()
        queued = pool.submit(next_writer)
        assert not next_started.wait(.1)
        exclusive_release.set(); cancelling.result(timeout=2); queued.result(timeout=2)
        assert next_started.is_set()
    @executor
    def special():
        pass
    assert guarded(special) is special
    with barrier():
        try:
            with barrier(exclusive=True):
                pass
        except RuntimeError:
            pass
        else:
            raise AssertionError("shared lock upgrade accepted")

with patch.object(settings, "reseller_cancellation_enabled", False), \
     patch("app.services.reseller_refund_fence._acquire", side_effect=AssertionError("unexpected lock")):
    with barrier():
        pass
print("PASS: ordinary/exclusive serialization, nested actions, no lock in disabled mode")
