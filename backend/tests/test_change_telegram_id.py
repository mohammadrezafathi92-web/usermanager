"""routers/auth.py's POST /api/auth/change-telegram-id - self-service
Telegram id link/unlink for the LOGGED-IN admin's own account (including
level-2 Admins and the superadmin, none of whom could set their OWN
telegram_id any other way).

Run:  python3 backend/tests/test_change_telegram_id.py

Bug reported 2026-09-09: routers/admins.py's update_admin is the only other
place AdminUser.telegram_id gets written, but its _scope_or_403 requires
`target.parent_admin_id == current.id` - never true for an account acting
on ITSELF - and it separately refuses to touch a superadmin row at all. So
a level-2 Admin (or the superadmin) had no way to link their own Telegram
account to the bot without asking someone else to edit the database
directly. This is the dedicated self-service fix, mirroring
change_username's own reasoning."""
from __future__ import annotations

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
from app.security import hash_password  # noqa: E402
from app.routers.auth import change_telegram_id, ChangeTelegramIdRequest  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

superadmin = models.AdminUser(username="root", hashed_password=hash_password("x"), is_superadmin=True)
admin2 = models.AdminUser(username="reseller1", hashed_password=hash_password("x"), is_superadmin=False, role="admin")
other = models.AdminUser(username="reseller2", hashed_password=hash_password("x"), is_superadmin=False, role="admin", telegram_id=999)
db.add_all([superadmin, admin2, other])
db.commit()
db.refresh(superadmin)
db.refresh(admin2)
db.refresh(other)

print("--- a level-2 Admin can set their OWN telegram_id (update_admin could never let them do this) ---")
res = change_telegram_id(ChangeTelegramIdRequest(telegram_id=111), admin=admin2, db=db)
check("ok", res["ok"], True)
check("returned id", res["telegram_id"], 111)
db.refresh(admin2)
check("committed to the db", admin2.telegram_id, 111)

print("\n--- even the superadmin can set their own (update_admin flatly refuses to touch a superadmin row) ---")
res2 = change_telegram_id(ChangeTelegramIdRequest(telegram_id=222), admin=superadmin, db=db)
check("ok", res2["ok"], True)
db.refresh(superadmin)
check("committed to the db", superadmin.telegram_id, 222)

print("\n--- an id already linked to a DIFFERENT admin is refused ---")
try:
    change_telegram_id(ChangeTelegramIdRequest(telegram_id=999), admin=admin2, db=db)
    check("raises on clash", False, True)
except HTTPException as exc:
    check("raises on clash", exc.status_code, 400)
db.refresh(admin2)
check("...admin2's own id unchanged", admin2.telegram_id, 111)

print("\n--- 0/None unlinks it ---")
res3 = change_telegram_id(ChangeTelegramIdRequest(telegram_id=0), admin=admin2, db=db)
check("ok", res3["ok"], True)
check("returned id is None", res3["telegram_id"], None)
db.refresh(admin2)
check("cleared in the db", admin2.telegram_id, None)

print("\n--- setting the SAME id an admin already has is a harmless no-op (not a clash with themselves) ---")
res4 = change_telegram_id(ChangeTelegramIdRequest(telegram_id=222), admin=superadmin, db=db)
check("ok", res4["ok"], True)
check("unchanged", res4["telegram_id"], 222)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
