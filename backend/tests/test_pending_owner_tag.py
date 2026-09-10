"""telegram_bot/handlers/admin_pending.py's _pending_summary/_owner_label/
_owner_labels, and routers/bot.py's new GET /admin-username/{id} endpoint
that backs them.

Run:  python3 backend/tests/test_pending_owner_tag.py

Feature requested 2026-09-10: "وقتی ادمین اصلی درخواست های در انتظار رو
میزنه همه درخواست های در انتظار بقیه ادمین ها هم براش میاد" - a superadmin
legitimately sees every Admin's/Seller's pending requests mixed together
(a superadmin owns the whole tree, same rule as everywhere else - see
routers/bot.py's get_admin_by_telegram), but with nothing on each request
saying whose customer it was for. Adds a "👤 مربوط به: <username>" tag,
resolved once per distinct owner for a list view (not once per item)."""
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


from fastapi import HTTPException  # noqa: E402

from app.database import Base, engine, SessionLocal  # noqa: E402
from app import models  # noqa: E402
from app.routers import bot as bot_router  # noqa: E402
from app.telegram_bot.handlers import admin_pending  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin1 = models.AdminUser(username="admin_one", hashed_password="x", is_superadmin=False, role="admin")
admin2 = models.AdminUser(username="admin_two", hashed_password="x", is_superadmin=False, role="admin")
db.add_all([admin1, admin2])
db.commit()
db.refresh(admin1)
db.refresh(admin2)

print("--- routers/bot.py's get_admin_username endpoint ---")
out = bot_router.get_admin_username(admin1.id, db=db)
check("returns the right username", out, {"username": "admin_one"})

try:
    bot_router.get_admin_username(999999, db=db)
    check("raises 404 for unknown id", False, True)
except HTTPException as exc:
    check("raises 404 for unknown id", exc.status_code, 404)

print("\n--- _pending_summary: no tag line when owner_label is None ---")
p = {"id": 1, "kind": "new", "telegram_id": 1, "telegram_username": "cust1",
     "package_name": "پکیج", "quota_gb": 10, "duration_days": 30, "price": 1000}
text = admin_pending._pending_summary(p, None)
check("no owner line", "مربوط به" in text, False)

print("\n--- _pending_summary: tag line shown when owner_label is given ---")
text = admin_pending._pending_summary(p, "admin_one")
check("owner line present", "👤 مربوط به: admin_one" in text, True)


async def _fake_get_admin_username(admin_id):
    return {admin1.id: "admin_one", admin2.id: "admin_two"}.get(admin_id)


async def main():
    admin_pending.api.get_admin_username = _fake_get_admin_username

    print("\n--- _owner_label: resolves a known id, None for an unowned request ---")
    check("resolves admin_one", await admin_pending._owner_label(admin1.id), "admin_one")
    check("None for no owner", await admin_pending._owner_label(None), None)

    print("\n--- _owner_labels: resolves each DISTINCT owner across a mixed batch ---")
    items = [
        {"owner_admin_id": admin1.id},
        {"owner_admin_id": admin1.id},
        {"owner_admin_id": admin2.id},
        {"owner_admin_id": None},
    ]
    labels = await admin_pending._owner_labels(items)
    check("batch labels", labels, {admin1.id: "admin_one", admin2.id: "admin_two"})


asyncio.run(main())

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
