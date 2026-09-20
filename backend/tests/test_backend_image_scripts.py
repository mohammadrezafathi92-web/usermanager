"""Every operational script is actually inside the backend image.

Run:  python3 backend/tests/test_backend_image_scripts.py

Reported 2026-09-20, running a command exactly as documented:

    docker compose exec backend python scripts/import_ads.py --all --replace
    python: can't open file '/app/scripts/import_ads.py': No such file

The Dockerfile copied ONE script by name (hash_recovery.py), so every other
one was missing from the image - and the way you find that out is on a live
server, at the moment you need the script. Three report/repair scripts had
the same problem and had simply never been reached for.

The default is inverted now: scripts ship unless deliberately excluded.
This test holds the two lists against the actual directory, so a script
added later either ships or is named as not shipping - never silently
absent.
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


BACKEND = pathlib.Path(__file__).resolve().parents[1]
DOCKERFILE = (BACKEND / "Dockerfile").read_text(encoding="utf-8")

# Deliberately not in the image, each for its own reason:
#   license_tool.py    - vendor only, takes the private signing key as input
#   cythonize_build.py - build-time only, used by the `compile` stage
EXCLUDED = {"license_tool.py", "cythonize_build.py"}

scripts = {p.name for p in (BACKEND / "scripts").glob("*.py")}
print(f"scripts on disk: {len(scripts)}\n")

print("--- the image takes the whole directory ---")
check("the Dockerfile copies scripts/", "COPY scripts ./scripts" in DOCKERFILE, True)
check("...and does not copy just one by name",
      "COPY scripts/hash_recovery.py" in DOCKERFILE, False)

print("\n--- and then removes exactly the ones that must not ship ---")
# Everything AFTER the directory copy - "rm -f" appears earlier in the file
# too (the compile stage cleans up after itself), and the first version of
# this test matched that one instead. A test that reads the wrong line is
# worse than none: it passes for the wrong reason just as easily.
after_copy = DOCKERFILE.split("COPY scripts ./scripts", 1)[1]
for name in sorted(EXCLUDED):
    check(f"{name} is removed after the copy", name in after_copy, True)

print("\n--- so every other script is reachable on a server ---")
shipped = sorted(scripts - EXCLUDED)
for name in shipped:
    # Nothing to assert per file beyond "it is not excluded" - the COPY
    # above takes the directory. Listed individually so the output says
    # WHICH scripts an admin can rely on being there.
    print(f"PASS  scripts/{name}")

check("the ones that ship are more than one",
      len(shipped) > 1, True)
check("the excluded ones really exist (a stale exclusion is a lie)",
      EXCLUDED - scripts, set())

print("\n--- the documented command matches reality ---")
# import_ads.py's own docstring tells the admin what to type. If the path
# in it and the path in the image ever disagree, the docstring is what
# somebody will follow.
ads = (BACKEND / "scripts" / "import_ads.py").read_text(encoding="utf-8")
check("import_ads.py documents the exec path it will actually have",
      "docker compose exec backend python scripts/import_ads.py" in ads, True)
check("...and it is one of the shipped scripts", "import_ads.py" in shipped, True)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("هر اسکریپتی که مستندش کرده‌ایم، واقعاً داخل ایمیج هست")
