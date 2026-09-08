"""routers/auth.py's POST /api/auth/change-username - self-service username
change for the LOGGED-IN admin's own account (including the superadmin).

Run:  python3 backend/tests/test_change_username.py

Feature added 2026-09-08: routers/admins.py's update_admin explicitly refuses
to touch a superadmin row at all, and its AdminUpdate schema has no username
field for anyone else either - so before this, NO admin had any way to
rename their own account. This mirrors the existing change-password
endpoint (re-enter current password) and, since the JWT's `sub` claim IS the
username (security.py's create/decode_access_token, deps.get_current_admin
looks the admin up BY username), returns a freshly-issued token for the new
username so the caller's current session does not immediately 401 on its
own successful rename.
"""
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
from app.security import hash_password, decode_access_token  # noqa: E402
from app.routers.auth import change_username, ChangeUsernameRequest  # noqa: E402

Base.metadata.create_all(bind=engine)
db = SessionLocal()

admin = models.AdminUser(username="oldname", hashed_password=hash_password("secret123"), is_superadmin=True)
db.add(admin)
db.commit()
db.refresh(admin)

other = models.AdminUser(username="taken", hashed_password=hash_password("x"), is_superadmin=False)
db.add(other)
db.commit()

print("--- wrong current password is refused ---")
try:
    change_username(ChangeUsernameRequest(current_password="wrong", new_username="newname"), admin=admin, db=db)
    check("raises on wrong password", False, True)
except HTTPException as e:
    check("raises on wrong password", e.status_code, 400)
check("...admin.username unchanged", admin.username, "oldname")

print("\n--- a username already used by another admin is refused ---")
try:
    change_username(ChangeUsernameRequest(current_password="secret123", new_username="taken"), admin=admin, db=db)
    check("raises on taken username", False, True)
except HTTPException as e:
    check("raises on taken username", e.status_code, 400)
check("...admin.username unchanged", admin.username, "oldname")

print("\n--- empty username is refused ---")
try:
    change_username(ChangeUsernameRequest(current_password="secret123", new_username="   "), admin=admin, db=db)
    check("raises on empty username", False, True)
except HTTPException as e:
    check("raises on empty username", e.status_code, 400)

print("\n--- happy path: renames, commits, and returns a token for the NEW name ---")
res = change_username(ChangeUsernameRequest(current_password="secret123", new_username="newname"), admin=admin, db=db)
check("ok", res["ok"], True)
check("returned username", res["username"], "newname")
db.refresh(admin)
check("committed to the db", admin.username, "newname")
check("new token's subject is the new username", decode_access_token(res["access_token"]), "newname")

print("\n--- renaming to the SAME username is a harmless no-op, still returns a fresh token ---")
res2 = change_username(ChangeUsernameRequest(current_password="secret123", new_username="newname"), admin=admin, db=db)
check("ok", res2["ok"], True)
check("username unchanged", res2["username"], "newname")

print("\n--- this even works for the superadmin, which routers/admins.py's update_admin refuses entirely ---")
check("this admin IS the superadmin", admin.is_superadmin, True)
check("...and was renamed anyway, via this dedicated self-service endpoint", admin.username, "newname")

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
