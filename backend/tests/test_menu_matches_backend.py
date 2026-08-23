"""The sidebar must not offer a section the backend will refuse.

Run:  python3 backend/tests/test_menu_matches_backend.py

A router that gates its whole prefix on a permission and a sidebar entry
that shows the same section unconditionally produce the worst possible
failure: the page opens, every request answers 403, and the user sees an
empty section with no explanation. That is what "the accounting section
hangs" was - the account (a Seller in the "فقط کاربران" group) had no
view_accounting, and حساب‌داری was in their menu anyway.

The frontend cannot import the backend, so the two lists are compared here
by reading both files. That is deliberately crude and deliberately cheap -
the only thing it has to catch is the two drifting apart.
"""
from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROUTERS = os.path.join(ROOT, "backend", "app", "routers")
SIDEBAR = os.path.join(ROOT, "frontend", "src", "components", "Sidebar.jsx")
APP = os.path.join(ROOT, "frontend", "src", "App.jsx")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# Which API prefix each sidebar path talks to. Only routes that map onto a
# router with its own prefix belong here; the rest are gated by role, not
# by permission, and are checked separately below.
PATH_TO_PREFIX = {
    "/accounting": "/api/accounting",
    "/discount-codes": "/api/discount-codes",
    "/tutorials": "/api/tutorials",
    "/users": "/api/users",
    "/nodes": "/api/nodes",
    "/packages": "/api/packages",
    "/radius-logs": "/api/radius-logs",
    "/settings": "/api/settings",
    "/admins": "/api/admins",
    "/ads": "/api/ads",
}


def router_permissions() -> dict[str, str]:
    """prefix -> permission required on the WHOLE router, where there is one."""
    out: dict[str, str] = {}
    for name in sorted(os.listdir(ROUTERS)):
        if not name.endswith(".py"):
            continue
        text = open(os.path.join(ROUTERS, name), encoding="utf-8").read()
        # Each APIRouter(...) call, brace-matched so a multi-line
        # declaration with comments inside it is read whole.
        for match in re.finditer(r"APIRouter\(", text):
            depth, i = 0, match.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            block = text[match.end() : i]
            prefix = re.search(r'prefix\s*=\s*"([^"]+)"', block)
            perm = re.search(r'require_permission\(\s*"(\w+)"\s*\)', block)
            if prefix and perm:
                out[prefix.group(1)] = perm.group(1)
    return out


def sidebar_permissions() -> dict[str, str | None]:
    """path -> the `perm` its sidebar entry is gated on (None = always shown)."""
    text = open(SIDEBAR, encoding="utf-8").read()
    out: dict[str, str | None] = {}
    for line in text.splitlines():
        m = re.search(r'\{\s*to:\s*"([^"]+)".*?perm:\s*(null|"[\w_]+"|\[[^\]]*\])', line)
        if m:
            raw = m.group(2)
            out[m.group(1)] = None if raw == "null" else raw.strip('"')
    return out


print("--- a gated router must have a gated menu entry ---")
routers = router_permissions()
sidebar = sidebar_permissions()

check("the sidebar was parsed at all", len(sidebar) >= 8, True)
check("gated routers were found at all", len(routers) >= 2, True)

for path, prefix in PATH_TO_PREFIX.items():
    required = routers.get(prefix)
    if required is None:
        continue  # router is not permission-gated - nothing to match
    if path not in sidebar:
        continue  # no menu entry for it
    shown_for = sidebar[path]
    if shown_for == required:
        print(f"PASS  {path} menu gate matches {prefix} ({required})")
    else:
        failures.append(f"{path} menu gate")
        print(
            f"FAIL  {path}: the menu shows it "
            + ("to everyone" if shown_for is None else f"on {shown_for!r}")
            + f", but {prefix} requires {required!r}.\n"
            f"        A Seller without {required!r} would open an empty section."
        )

print("\n--- and the route itself must be guarded, not just the menu ---")
app_text = open(APP, encoding="utf-8").read()
for path, prefix in PATH_TO_PREFIX.items():
    required = routers.get(prefix)
    if required is None or path not in sidebar:
        continue
    # The <Route path="..."> and the guard wrapping its element.
    m = re.search(
        r'<Route\s+path="' + re.escape(path) + r'"\s+element=\{\s*<(\w+)',
        app_text,
    )
    guard = m.group(1) if m else "(route not found)"
    if guard == "PermRoute":
        # ...and on the right permission.
        seg = app_text[m.start() : m.start() + 400]
        ok = f'perm="{required}"' in seg
        check(f"{path} route guarded on {required}", ok, True)
    else:
        failures.append(f"{path} route guard")
        print(
            f"FAIL  {path}: wrapped in <{guard}>, so typing the URL still opens "
            f"a page whose every request answers 403 ({required} needed)."
        )

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("منو و بک‌اند هم‌خوان هستند")
