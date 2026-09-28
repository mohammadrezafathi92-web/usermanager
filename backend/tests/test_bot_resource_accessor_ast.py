"""Phase C (docs/api-key-scope-audit-2026-09-27.md) - the resource-accessor
side of "no other way to reach these models" (the decorator in
test_bot_route_policy_coverage.py covers the capability side).

Two things are actually checked here, precisely:

1. telegram_bot/panel_bridge.py - the file the audit's bypasses were
   FOUND in (get_package_files/get_tutorial_media/get_tutorial_software_
   file/get_admin_telegram_id/get_admin_username all used to query
   models.PackageFile/TutorialMedia/TutorialSoftware/AdminUser directly,
   with zero authorization) - has ZERO direct db.query()/db.get() call on
   any of the six gated models left. Every one of those methods now calls
   into services/bot_resources.py's accessors instead.

2. services/bot_resources.py's own top-level function names are EXACTLY
   the 11 named accessors - not a subset, not a superset. A 12th function
   added under a plausible-sounding name would be caught here even if it
   never gets called from anywhere.

routers/bot.py is deliberately NOT held to the same blanket "zero direct
access" rule attempted for panel_bridge.py above: reading the real file
turned up several genuine business-logic lookups that are not
authorization decisions at all (e.g. _charge_seller loading the AdminUser
to bill via an ALREADY-authorized user's own owner_admin_id,
_ensure_telegram_can_buy's deliberately panel-wide one-time-package check,
create_user/purchase_package/renew_service loading the Package the
customer is buying to read its price/quota rather than to decide
visibility). Every endpoint's own AUTHORIZATION decision is still proven
correct, per-endpoint, by test_bot_route_policy_coverage.py (the
capability gate) and by routers/bot.py itself calling bot_resources'
accessors for every object a principal's access actually turns on (see
that file's own diff) - but claiming a blanket AST rule for this file
would either be false or would require inventing a large, unprincipled
exceptions list, which is exactly the "closed set, not a growing
allowlist" property this design otherwise achieves. Said plainly here
rather than overclaimed.

Run:  python3 backend/tests/test_bot_resource_accessor_ast.py
"""
from __future__ import annotations

import ast
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

ALLOWED_FUNCS = {
    "_get_user_or_403", "_list_users_query",
    "_get_package_or_403", "_list_packages_query",
    "_get_tutorial_or_403", "_list_tutorials_query",
    "_get_payment_card_or_403",
    "_get_node_or_403", "_scoped_node_ids",
    "_get_admin_by_telegram_or_403", "_get_admin_or_403",
}
GATED_MODELS = {"User", "Package", "Tutorial", "PaymentCard", "Node", "AdminUser"}


def _gated_model_calls(tree: ast.AST):
    """Yields every Call node shaped like db.query(models.X)/db.get(models.X, ...)
    (or db.query(models.X.col) - .query() may be given an attribute access
    ON the model too, e.g. models.User.id) where X is a gated model name."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr in ("query", "get")):
            continue
        if not node.args:
            continue
        first = node.args[0]
        # models.X  or  models.X.something
        target = first
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Attribute):
            target = target.value  # models.X.col -> models.X
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "models"
            and target.attr in GATED_MODELS
        ):
            yield node


print("=" * 60)
print("--- telegram_bot/panel_bridge.py: zero direct access to gated models ---")
print("=" * 60)

bridge_path = os.path.join(APP_DIR, "telegram_bot", "panel_bridge.py")
bridge_tree = ast.parse(open(bridge_path, encoding="utf-8").read(), filename=bridge_path)
bridge_hits = list(_gated_model_calls(bridge_tree))
check("panel_bridge.py has zero db.query(models.<gated>)/db.get(models.<gated>, ...) "
      "calls left - the five bypasses the audit found (get_package_files/get_tutorial_media/"
      "get_tutorial_software_file/get_admin_telegram_id/get_admin_username) all now call "
      "into services/bot_resources.py instead",
      len(bridge_hits), 0)
if bridge_hits:
    for node in bridge_hits:
        print(f"        unexpected direct access at panel_bridge.py:{node.lineno}")

print("\n" + "=" * 60)
print("--- services/bot_resources.py: exactly the 11 named accessors, no more, no fewer ---")
print("=" * 60)

resources_path = os.path.join(APP_DIR, "services", "bot_resources.py")
resources_tree = ast.parse(open(resources_path, encoding="utf-8").read(), filename=resources_path)
top_level_funcs = {
    node.name for node in ast.iter_child_nodes(resources_tree)
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
}
check("bot_resources.py's top-level function names are EXACTLY ALLOWED_FUNCS - "
      "a 12th function under a plausible name would fail this even if uncalled, "
      "and a removed one would fail it too",
      top_level_funcs, ALLOWED_FUNCS)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
