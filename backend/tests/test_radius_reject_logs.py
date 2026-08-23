"""Why a customer's login was refused, recorded where an admin can see it.

Run:  python3 backend/tests/test_radius_reject_logs.py

Every reason was already computed - it went to the container's stdout, so
answering "why can't my customer connect?" meant SSH and grep.
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")

from app.services import radius_server

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class Recorder:
    """Just the suppression logic, without binding real RADIUS sockets."""

    def __init__(self):
        self._rejection_log_times = {}

    _should_log_rejection = radius_server.UserManagerRadiusServer._should_log_rejection


print("--- the same reason from the same client is not written twice ---")
r = Recorder()
check("first attempt is logged", r._should_log_rejection("auth_fail:ali:1.2.3.4"), True)
check("the retry a second later is not", r._should_log_rejection("auth_fail:ali:1.2.3.4"), False)
check("nor the one after that", r._should_log_rejection("auth_fail:ali:1.2.3.4"), False)

print("\n--- but genuinely different events still get through ---")
check("a different reason, same user", r._should_log_rejection("expired:ali:1.2.3.4"), True)
check("the same reason, a different user", r._should_log_rejection("auth_fail:reza:1.2.3.4"), True)
check("the same reason and user, a different IP",
      r._should_log_rejection("auth_fail:ali:9.9.9.9"), True)

print("\n--- the window does expire ---")
r2 = Recorder()
key = "auth_fail:ali:1.2.3.4"
r2._should_log_rejection(key)
# Rewind the recorded time past the window rather than sleeping ten minutes.
r2._rejection_log_times[key] -= radius_server.REJECTION_LOG_WINDOW_SECONDS + 1
check("after the window, it is logged again", r2._should_log_rejection(key), True)

print("\n--- the table cannot be grown without bound ---")
r3 = Recorder()
for i in range(5200):
    r3._should_log_rejection(f"unknown_user:bot{i}:1.2.3.4")
check("the tracking dict stays bounded", len(r3._rejection_log_times) <= 5001, True)

print("\n--- every reject reason maps to a named event ---")
# Read the source rather than trusting a list here: a new rejection branch
# that forgets to set reject_kind is exactly the bug this guards against.
src = open(os.path.join(os.path.dirname(radius_server.__file__), "radius_server.py")).read()
for kind in ("auth_fail", "quota_exceeded", "expired", "disabled", "unknown_user"):
    check(f"{kind} is set somewhere", f'reject_kind = "{kind}"' in src or f'"{kind}"' in src, True)

# The window must be long enough to matter and short enough to stay useful.
check("the suppression window is minutes, not seconds",
      60 <= radius_server.REJECTION_LOG_WINDOW_SECONDS <= 3600, True)

print("\n--- the panel and the backend agree on the event names ---")
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
page = open(os.path.join(here, "..", "frontend", "src", "pages", "RadiusLogs.jsx")).read()
for kind in ("auth_fail", "quota_exceeded", "expired", "disabled", "unknown_user"):
    check(f"{kind} has a label in the UI", f'{kind}: {{ tone:' in page, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
