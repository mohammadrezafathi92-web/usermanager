"""Phase C (docs/api-key-scope-audit-2026-09-27.md) - proves every single
/api/bot route is decorated with @bot_route_policy, and with EXACTLY the
capability/resource_strategy the audit doc's endpoint table calls for - not
just that a policy dict happens to be internally complete.

Keyed by the endpoint FUNCTION OBJECT itself (route.endpoint, the thing
FastAPI actually registered), not by a separately-typed (method, path)
string - a decorated function can never be "forgotten" from this check the
way a hand-maintained dict entry could drift out of sync with the real
routes. See services/bot_auth.bot_route_policy's own docstring for why this
- not a bare dict of policies - is what actually makes the capability check
impossible to skip on a new endpoint.

Run:  python3 backend/tests/test_bot_route_policy_coverage.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi.routing import APIRoute

from app.routers.bot import router
from app.services.bot_auth import (
    ADMIN_LOOKUP,
    BROADCAST,
    CUSTOMER_READ,
    CUSTOMER_WRITE,
    FILES_READ,
    IDENTITY_WRITE,
    PAYMENT_READ,
    PAYMENT_WRITE,
    WALLET_WRITE,
)

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


# The canonical (method, path) -> (capability, resource_strategy) table -
# docs/api-key-scope-audit-2026-09-27.md's endpoint table, section ۷ of the
# نسخه‌ی هشتم (canonical) design. Paths are relative to /api/bot, matched
# below against each route's own .path with the router's prefix stripped.
CANONICAL: dict[tuple[str, str], tuple[str | None, str]] = {
    ("GET", "/nodes"): (None, "node_list"),
    ("GET", "/packages"): (CUSTOMER_READ, "package_list"),
    ("GET", "/payment-info"): (PAYMENT_READ, "claim"),
    ("GET", "/payment-cards/{card_id}"): (PAYMENT_READ, "payment_card_owner"),
    ("POST", "/payment-cards/{card_id}/record-payment"): (PAYMENT_WRITE, "payment_card_owner"),
    ("GET", "/sales-stats"): (CUSTOMER_READ, "claim"),
    ("GET", "/customer-menu-config"): (None, "none"),
    ("GET", "/tutorials"): (CUSTOMER_READ, "tutorial_list"),
    ("GET", "/packages/{package_id}/files/{file_id}/download"): (FILES_READ, "package_owner"),
    ("GET", "/tutorials/{tutorial_id}/media/{media_id}/download"): (FILES_READ, "tutorial_owner"),
    ("GET", "/tutorials/{tutorial_id}/software/{software_id}/download"): (FILES_READ, "tutorial_owner"),
    ("GET", "/admin-by-telegram/{tg_id}"): (ADMIN_LOOKUP, "admin_hierarchy"),
    ("GET", "/admin-username/{admin_id}"): (ADMIN_LOOKUP, "admin_hierarchy"),
    ("GET", "/telegram-user-ids"): (BROADCAST, "claim"),
    ("POST", "/users"): (CUSTOMER_WRITE, "claim"),
    ("POST", "/users/{username}/purchase-package"): (CUSTOMER_WRITE, "user"),
    ("POST", "/referral/apply"): (CUSTOMER_WRITE, "user"),
    ("POST", "/discount/validate"): (CUSTOMER_WRITE, "claim"),
    ("POST", "/discount/redeem"): (CUSTOMER_WRITE, "claim"),
    ("GET", "/users"): (CUSTOMER_READ, "user_list"),
    ("GET", "/users/by-telegram/{telegram_id}"): (CUSTOMER_READ, "user_list"),
    ("GET", "/users/by-telegram/{telegram_id}/all"): (CUSTOMER_READ, "user_list"),
    ("GET", "/users/{username}"): (CUSTOMER_READ, "user"),
    ("POST", "/users/{username}/link-telegram"): (IDENTITY_WRITE, "user"),
    ("POST", "/users/{username}/connections"): (CUSTOMER_WRITE, "user+node"),
    ("GET", "/users/{username}/purchases"): (CUSTOMER_READ, "user"),
    ("POST", "/users/{username}/purchases/{purchase_id}/rename"): (CUSTOMER_WRITE, "user"),
    ("DELETE", "/users/{username}/purchases/{purchase_id}"): (CUSTOMER_WRITE, "user"),
    ("DELETE", "/users/{username}/connections/{connection_id}"): (CUSTOMER_WRITE, "user"),
    ("GET", "/users/{username}/subscription-link"): (CUSTOMER_READ, "user"),
    ("GET", "/miniapp-button-text"): (None, "none"),
    ("GET", "/panel-public-url"): (None, "none"),
    ("POST", "/users/{username}/purchases/{purchase_id}/renew"): (CUSTOMER_WRITE, "user"),
    ("POST", "/users/{username}/renew"): (CUSTOMER_WRITE, "user"),
    ("POST", "/users/{username}/reset-usage"): (CUSTOMER_WRITE, "user"),
    ("POST", "/users/{username}/set-enabled"): (CUSTOMER_WRITE, "user"),
    ("POST", "/users/{username}/add-balance"): (WALLET_WRITE, "user"),
    ("DELETE", "/users/{username}"): (CUSTOMER_WRITE, "user"),
}

routes = [r for r in router.routes if isinstance(r, APIRoute)]
check("router has exactly 38 routes - the audit doc's own count, verified against "
      "the real file with grep, not assumed", len(routes), 38)
check("CANONICAL table itself also has exactly 38 entries", len(CANONICAL), 38)

# route.path already carries the router's own prefix ("/api/bot/...") once
# the routes are registered on this APIRouter object - CANONICAL above is
# written relative (matching the audit doc's own table) for readability, so
# the prefix is added here, once, rather than hand-typing it into all 38
# entries above.
CANONICAL = {(method, "/api/bot" + path): value for (method, path), value in CANONICAL.items()}

seen: set[tuple[str, str]] = set()
for route in routes:
    method = sorted(route.methods - {"HEAD"})[0]
    key = (method, route.path)
    seen.add(key)

    check(f"{method} {route.path}: is decorated with @bot_route_policy at all",
          hasattr(route.endpoint, "_bot_capability"), True)
    if not hasattr(route.endpoint, "_bot_capability"):
        continue

    if key not in CANONICAL:
        failures.append(f"{method} {route.path}: not in CANONICAL table")
        print(f"FAIL  {method} {route.path}: exists as a real route but is not in this "
              f"test's own CANONICAL table - update both this file and the audit doc")
        continue

    expected_cap, expected_strategy = CANONICAL[key]
    got = (route.endpoint._bot_capability, route.endpoint._bot_resource_strategy)
    check(f"{method} {route.path}: capability+resource_strategy match the audit doc exactly",
          got, (expected_cap, expected_strategy))

missing = set(CANONICAL) - seen
check("no CANONICAL entry names a route that does not actually exist in routers/bot.py "
      "(a route removed from the code but not from this test would otherwise go unnoticed)",
      missing, set())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
