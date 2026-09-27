"""Characterization test (2026-09-27 audit, stage 3 continued): what the
IN-PROCESS bot caller (telegram_bot/panel_bridge.py) can actually do TODAY,
via its real public interface (`api.xxx(...)`) - not by calling
routers/bot.py directly. This is the third caller shape, alongside the
HTTP-key path already characterized by test_bot_api_key_cross_tenant.py and
the pure visibility-filter logic already characterized by
test_bot_scope.py.

Nothing here proposes a fix. This locks in today's baseline so that once
Product approves wiring services/bot_auth.py's BotPrincipal.internal() into
panel_bridge.py, a reviewer can see exactly what changed against a known
"before" - deliberately, not by accident.

Run:  python3 backend/tests/test_bot_inprocess_trust_characterization.py
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.services import hierarchy
from app.telegram_bot import panel_bridge
from app.telegram_bot.config import config

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def run(coro):
    return asyncio.run(coro)


# StaticPool + a shared engine: panel_bridge._call() opens its OWN
# SessionLocal() per call (see its docstring) using the name `SessionLocal`
# it already imported (`from ..database import SessionLocal`) at module
# load time - reassigning app.database.SessionLocal afterward would NOT
# reach that already-bound reference, so the module-local name inside
# panel_bridge itself has to be repointed instead.
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
panel_bridge.SessionLocal = sessionmaker(bind=engine)
db = panel_bridge.SessionLocal()

admin_a = models.AdminUser(username="admin_a", hashed_password="x", role=hierarchy.ROLE_ADMIN)
admin_b = models.AdminUser(username="admin_b", hashed_password="x", role=hierarchy.ROLE_ADMIN)
db.add_all([admin_a, admin_b])
db.commit()
for a in (admin_a, admin_b):
    hierarchy.rebuild_path(db, a)
db.commit()

print("=" * 60)
print("--- the SHARED bot (config.bot_owner_admin_id = None) ---")
print("=" * 60)

config.bot_owner_admin_id = None
run(panel_bridge.api.create_user("shared_signup"))
shared_signup = db.query(models.User).filter(models.User.username == "shared_signup").first()
check("a plain create_user call with no explicit owner lands unowned (owner_admin_id=None) - "
      "the shared bot's normal, correct behavior",
      shared_signup.owner_admin_id if shared_signup else "MISSING", None)

run(panel_bridge.api.create_user("shared_explicit_a", owner_admin_id=admin_a.id))
explicit_a = db.query(models.User).filter(models.User.username == "shared_explicit_a").first()
check("the SHARED bot can still create a user under ANY admin if the handler code "
      "passes an explicit owner_admin_id - _scope() only fills in a DEFAULT, it "
      "is not a ceiling on what the shared bot may claim",
      explicit_a.owner_admin_id if explicit_a else "MISSING", admin_a.id)

print("\n" + "=" * 60)
print("--- a DEDICATED admin bot (config.bot_owner_admin_id = admin_a.id) ---")
print("=" * 60)

config.bot_owner_admin_id = admin_a.id
try:
    run(panel_bridge.api.create_user("dedicated_default"))
    dedicated_default = db.query(models.User).filter(models.User.username == "dedicated_default").first()
    check("a dedicated admin bot's plain create_user call (no explicit owner) is "
          "filled in with ITS OWN admin id by _scope() - this is the one place "
          "the isolation genuinely works today",
          dedicated_default.owner_admin_id if dedicated_default else "MISSING", admin_a.id)

    print("\n--- but nothing stops that SAME dedicated bot's code from claiming a "
          "DIFFERENT tenant if it explicitly passes one ---")
    run(panel_bridge.api.create_user("dedicated_but_claims_b", owner_admin_id=admin_b.id))
    crossed = db.query(models.User).filter(models.User.username == "dedicated_but_claims_b").first()
    check("_scope() never overrides an EXPLICIT owner_admin_id, even when the calling "
          "thread's own config identity is a different admin - there is no ceiling, "
          "only a default. Confirms the audit doc's point: the in-process path needs "
          "the SAME central enforcement as the HTTP path, not a separate, weaker one",
          crossed.owner_admin_id if crossed else "MISSING", admin_b.id)

    print("\n--- reading a DIFFERENT tenant's customer, with no explicit owner override ---")
    b_cust = db.query(models.User).filter(models.User.username == "shared_explicit_a").first()
    # admin_a's own dedicated bot, asking for a customer under admin_a itself -
    # this one SHOULD and does succeed (own tenant).
    fetched = run(panel_bridge.api.get_user("shared_explicit_a"))
    check("a dedicated bot CAN read its own tenant's customer with no explicit owner",
          fetched.get("username") if fetched else None, "shared_explicit_a")
finally:
    config.bot_owner_admin_id = None

print("\n" + "=" * 60)
print("--- link_telegram / add_balance: no owner_admin_id parameter exists on the "
      "panel_bridge interface EITHER - not just on the HTTP router ---")
print("=" * 60)

import inspect  # noqa: E402

link_sig = inspect.signature(panel_bridge.PanelBridge.link_telegram)
balance_sig = inspect.signature(panel_bridge.PanelBridge.add_balance)
check("panel_bridge.PanelBridge.link_telegram has no owner_admin_id parameter - "
      "confirms this gap is NOT an HTTP-layer oversight alone, the in-process "
      "interface never had a place to put one either",
      "owner_admin_id" in link_sig.parameters, False)
check("same for add_balance", "owner_admin_id" in balance_sig.parameters, False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
