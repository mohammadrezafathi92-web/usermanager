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


# --------------------------------------------------------------------------
# The white page of 2026-09-14, and the one line that caused it.
#
# Pressing «ساخت سرویس تست» blanked the page. The trial card spread
# emptyForm, which carries "" for the optional numeric fields (an empty
# <input> is ""), and the backend's Optional[int] refuses that - a 422
# whose `detail` is a LIST of error objects. The card then did
#
#     setError(err?.response?.data?.detail || fallback)
#
# putting an ARRAY into state and then into JSX, and React throws on an
# object rendered as a child: the whole page goes.
#
# Both halves are asserted, because either alone would have hidden it:
# utils.errorText turns any of FastAPI's shapes into a string, and the
# trial card no longer sends "" for a field typed as a number.
import pathlib as _pathlib  # noqa: E402

_pkg = _pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "Packages.jsx"
_src = _pkg.read_text(encoding="utf-8")

check("the trial card reports errors through errorText, not a raw detail",
      "setError(errorText(err" in _src, True)
check("...and never puts a raw detail into state",
      "response?.data?.detail ||" in _src, False)
check("it sends null, not \"\", for the optional numeric fields",
      "group_id: null," in _src, True)

_utils = _pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src" / "utils.js"
_u = _utils.read_text(encoding="utf-8")
check("errorText handles FastAPI's array-shaped detail",
      "Array.isArray(detail)" in _u, True)

# And the question that came with the bug report: where protocols are
# chosen. A trial that picks servers but defaults the protocol has decided
# something the admin never said.
check("the trial picker offers protocols per server",
      "protocolsForType(n.type)" in _src, True)
check("...and carries the choice into the package",
      "chosen.map(({ node_id, protocol })" in _src, True)


# --------------------------------------------------------------------------
# The third white page, 2026-09-17: «دوباره صفحه کاربران لود نمیشه». An icon
# was used on a page and never added to the import line. JSX compiles that
# to React.createElement(Name, ...) - valid JavaScript until it runs, when
# it throws ReferenceError and React unmounts the whole tree. Green build,
# blank page.
#
# check-imports.mjs catches it, and is only useful if it actually runs.
check("check-imports.mjs exists",
      (_pathlib.Path(__file__).resolve().parents[2] / "frontend" / "scripts" / "check-imports.mjs").is_file(),
      True)
_pkg_json = __import__("json").loads(
    (_pathlib.Path(__file__).resolve().parents[2] / "frontend" / "package.json").read_text(encoding="utf-8")
)
check("...and the build runs it", "check-imports.mjs" in _pkg_json["scripts"]["build"], True)
check("...alongside the other two guards",
      all(name in _pkg_json["scripts"]["build"]
          for name in ("check-hooks.mjs", "check-i18n.mjs")), True)
