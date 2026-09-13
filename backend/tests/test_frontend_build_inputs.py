"""Everything `npm run build` needs must be COPYed into the frontend image.

Run:  python3 backend/tests/test_frontend_build_inputs.py

2026-09-13: a build step was added to package.json (scripts/check-hooks.mjs)
without the matching COPY in frontend/Dockerfile. It passed locally, where
the whole repo is on disk, and failed only inside Docker with "Cannot find
module" - which is the one place nobody runs by hand before pushing.

The panel's self-update caught it and kept the old version running, which is
exactly what that design is for. But "the deploy notices" is a slow and
public way to learn this; the Dockerfile and package.json can be compared
without building anything.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = pathlib.Path(__file__).resolve().parents[2] / "frontend"
failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


dockerfile = (ROOT / "Dockerfile").read_text()
package = json.loads((ROOT / "package.json").read_text())
build_cmd = package["scripts"]["build"]

# Only the build stage matters - the runtime stage copies the finished dist.
build_stage = dockerfile.split("FROM nginx")[0]
copied = set()
for match in re.finditer(r"^COPY\s+(.+?)\s+\S+\s*$", build_stage, re.M):
    for part in match.group(1).split():
        copied.add(part.strip("./").split("/")[0])

referenced = {
    path.strip("./").split("/")[0]
    for path in re.findall(r"([\w./-]+\.(?:mjs|js|cjs))", build_cmd)
}

print("build:", build_cmd)
check("every file the build command runs is in the image",
      sorted(referenced - copied), [])
check("the hook check is part of the build, not an optional extra",
      "check-hooks" in build_cmd, True)
check("...and the script it names actually exists",
      (ROOT / "scripts" / "check-hooks.mjs").exists(), True)

# The sources themselves, which is what the build compiles.
for needed in ("src", "index.html", "package.json"):
    check(f"{needed} is copied", needed in copied, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("هرچه بیلد فرانت‌اند لازم دارد داخل ایمیج کپی می‌شود")
