"""Admins and sellers get a display name separate from their login username.

Run:  python3 backend/tests/test_admin_display_name.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "admin-display-name-test-only-0123456789")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deps, models
from app.database import get_db
from app.routers import admins as admins_router

failures: list[str] = []


def check(label, got, expected):
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001 - a missing API is one FAIL, not an abort
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, tree_path="/1/")
db.add(root); db.commit()

app = FastAPI()
app.include_router(admins_router.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[deps.get_current_admin] = lambda: db.get(models.AdminUser, root.id)
client = TestClient(app)

created = client.post("/api/admins", json={"username": "09120000000", "password": "secret123", "role": "seller",
                                           "parent_admin_id": root.id, "display_name": "  علی رضایی  "})
check("create accepts a display name", lambda: created.status_code, 200)
admin_id = created.json().get("id") if created.status_code == 200 else None
check("display name is stored trimmed", lambda: created.json().get("display_name"), "علی رضایی")
check("username stays the login", lambda: created.json().get("username"), "09120000000")
check("listing shows it", lambda: next(a["display_name"] for a in client.get("/api/admins").json() if a["id"] == admin_id),
      "علی رضایی")
check("update changes it", lambda: client.put(f"/api/admins/{admin_id}", json={"display_name": "Ali"}).json()["display_name"],
      "Ali")
check("an update without the field leaves it", lambda: client.put(f"/api/admins/{admin_id}", json={"permissions": []}
                                                                   ).json()["display_name"], "Ali")
check("blank clears it", lambda: client.put(f"/api/admins/{admin_id}", json={"display_name": "  "}).json()["display_name"],
      None)
check("too long is refused", lambda: client.put(f"/api/admins/{admin_id}", json={"display_name": "x" * 129}).status_code, 422)
check("username unchanged throughout", lambda: db.get(models.AdminUser, admin_id).username, "09120000000")

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall admin display name checks passed")
