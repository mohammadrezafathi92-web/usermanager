"""Auto-incrementing version: MAJOR.MINOR from the VERSION file, patch from
the commit count, so every update bumps the number with no manual edit.

Run:  python3 backend/tests/test_version.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app.services import version as v

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


print("--- the base + count -> version rule ---")
check("a normal 3-part base keeps major.minor, count becomes the patch",
      v._auto_version("1.3.0", 167), "1.3.167")
check("a 2-part base works too", v._auto_version("1.3", 200), "1.3.200")
check("a 1-part base fills a zero minor", v._auto_version("2", 5), "2.0.5")
check("count of zero is fine", v._auto_version("1.3.0", 0), "1.3.0")
check("a big count", v._auto_version("1.4.0", 99999), "1.4.99999")

print("\n--- graceful fallback when git is not available ---")
check("no count: the raw VERSION string is used unchanged",
      v._auto_version("1.3.0", None), "1.3.0")
check("no base at all: None (get_build_info then uses DEFAULT_VERSION)",
      v._auto_version(None, 10), None)
check("no base, no count: None", v._auto_version(None, None), None)

print("\n--- whitespace/newline in the VERSION file is tolerated ---")
check("trailing newline stripped", v._auto_version("1.3.0\n", 12), "1.3.12")
check("surrounding spaces stripped", v._auto_version("  1.3.0  ", 12), "1.3.12")

print("\n--- monotonic: more commits => a higher patch ---")
a = v._auto_version("1.3.0", 100)
b = v._auto_version("1.3.0", 101)
check("101 commits sorts after 100",
      int(b.split(".")[-1]) > int(a.split(".")[-1]), True)

print("\n--- the real repo produces a sane version ---")
# In this checkout git IS available, so the version should be MAJOR.MINOR.N
# with N > 0, and never the DEFAULT_VERSION fallback.
os.environ["HOST_PROJECT_DIR"] = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
v.get_build_info.cache_clear()
info = v.get_build_info()
parts = info["version"].split(".")
check("version has three dotted parts", len(parts) == 3, True)
check("...all numeric", all(p.isdigit() for p in parts), True)
check("...the patch (commit count) is positive", int(parts[-1]) > 0, True)
check("...and it is not the 0.0.0 fallback", info["version"] != v.DEFAULT_VERSION, True)
check("a commit id is also present", bool(info["commit_short"]), True)

print("\n--- an explicit APP_VERSION override still wins ---")
os.environ["APP_VERSION"] = "9.9.9"
v.get_build_info.cache_clear()
check("APP_VERSION overrides the auto value", v.get_build_info()["version"], "9.9.9")
del os.environ["APP_VERSION"]
v.get_build_info.cache_clear()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
