"""telegram_bot/admin_scope.py's resolve_admin_scope - a dedicated per-admin/
per-seller bot (AdminUser.own_bot_token) must show the admin menu ONLY to
that bot's own linked telegram_id, never to some OTHER admin/seller who
happens to be linked elsewhere in the panel.

Run:  python3 backend/tests/test_admin_scope_bot_isolation.py

Bug reported 2026-09-10: "وقتی یه ایدی ادمینِ یه بات باشه وارد یه بات دیگه
بشه اونجا هم منو ادمین رو میبینه" (when an id is an admin of one bot and
enters a different bot, they see the admin menu there too). Root cause:
get_admin_by_telegram looks a telegram id up PANEL-WIDE, with no idea which
bot instance is asking - resolve_admin_scope returned that global result
unconditionally. runner.py's start_admin_bot already scopes
config.admin_ids to exactly the bot owner's own linked id ("nobody else
gets the admin command menu on THIS bot"), but resolve_admin_scope never
checked it before this fix."""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


from app.telegram_bot import admin_scope  # noqa: E402
from app.telegram_bot.config import config  # noqa: E402

ADMIN_A_ID, ADMIN_A_TG = 10, 111
ADMIN_B_ID, ADMIN_B_TG = 20, 222
UNLINKED_ADMIN_TG = 333  # in config.admin_ids but resolves to no AdminUser


async def _fake_get_admin_by_telegram(tg_id):
    if tg_id == ADMIN_A_TG:
        return {"id": ADMIN_A_ID, "username": "admin_a", "is_superadmin": False, "role": "admin",
                "owner_ids": [ADMIN_A_ID], "include_unowned": False}
    if tg_id == ADMIN_B_TG:
        return {"id": ADMIN_B_ID, "username": "admin_b", "is_superadmin": False, "role": "admin",
                "owner_ids": [ADMIN_B_ID], "include_unowned": False}
    return None


async def main():
    admin_scope.api.get_admin_by_telegram = _fake_get_admin_by_telegram

    print("--- shared/global bot (bot_owner_admin_id=None): any linked admin gets their own scope, unchanged ---")
    config.bot_owner_admin_id = None
    config.admin_ids = set()
    scope = await admin_scope.resolve_admin_scope(ADMIN_B_TG)
    check("admin B recognized on the shared bot", scope is not None and scope["owner_admin_id"], ADMIN_B_ID)

    print("\n--- Admin A's OWN dedicated bot: Admin A's own linked id is allowed ---")
    config.bot_owner_admin_id = ADMIN_A_ID
    config.admin_ids = {ADMIN_A_TG}
    scope = await admin_scope.resolve_admin_scope(ADMIN_A_TG)
    check("owner allowed on their own bot", scope is not None and scope["owner_admin_id"], ADMIN_A_ID)

    print("\n--- Admin A's OWN dedicated bot: Admin B (a DIFFERENT, unrelated admin) is refused - THE BUG ---")
    scope = await admin_scope.resolve_admin_scope(ADMIN_B_TG)
    check("admin B treated as a plain customer on A's bot", scope, None)

    print("\n--- Admin A's OWN dedicated bot: a random customer id is still just None, as before ---")
    scope = await admin_scope.resolve_admin_scope(999999)
    check("unrelated customer", scope, None)

    print("\n--- config-only fallback (id in admin_ids, no panel account at all) still works on a dedicated bot ---")
    config.admin_ids = {UNLINKED_ADMIN_TG}
    scope = await admin_scope.resolve_admin_scope(UNLINKED_ADMIN_TG)
    check("config-only admin still recognized", scope is not None and scope["role"], "config")

    # cleanup - RuntimeConfig is threading.local, but be tidy anyway
    config.bot_owner_admin_id = None
    config.admin_ids = set()


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
