"""Real panel/remote/in-process/Mini App boundaries; no live network or keys."""
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "isolated-licence-admission-test-not-production")

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from jose import jwt
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deps, models
from app.config import settings
from app.database import get_db
from app.routers import miniapp, receipt_approvals
from app.security import create_access_token
from app.services import bot_auth, license_guard, license_state, licensing
from app.telegram_bot import panel_bridge

engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
with Session() as db:
    db.add_all([
        models.AdminUser(username="root", hashed_password="unused", is_superadmin=True),
        models.AdminUser(username="seller", hashed_password="unused", is_superadmin=False),
        models.ApiKey(key="isolated-key", label="test", enabled=True),
    ])
    db.commit()


def sessions():
    with Session() as db:
        yield db


app = FastAPI()
app.dependency_overrides[get_db] = sessions


@app.get("/api/private")
def private(admin=Depends(deps.get_current_admin)):
    return {"username": admin.username}


@app.get("/api/auth/me")
def me(admin=Depends(deps.get_current_admin)):
    return {"username": admin.username}


@app.get("/api/license/status")
def status(admin=Depends(deps.require_superadmin)):
    return {"available": True}


@app.put("/api/license/key")
def key(admin=Depends(deps.require_superadmin)):
    return {"available": True}


@app.post("/api/license/future-admin-route")
def future(admin=Depends(deps.require_superadmin)):
    return {"unexpected": True}


for method, path in [
    ("POST", "/api/bot/users"), ("GET", "/api/bot/users"),
    ("DELETE", "/api/bot/users/customer/purchases/1"),
    ("POST", "/api/accounting/receipt-approvals/11111111-1111-1111-1111-111111111111/finalize"),
]:
    def bot_endpoint(principal=Depends(deps.get_bot_principal)):
        return {"accepted": True}
    app.add_api_route(path, bot_endpoint, methods=[method])


@app.post("/api/miniapp/checkout")
def checkout(visitor=Depends(miniapp.current_visitor)):
    return {"accepted": True}


@app.get("/api/miniapp/home")
def home(visitor=Depends(miniapp.current_visitor)):
    return {"accepted": True}


client = TestClient(app)
normal = {"Authorization": "Bearer " + create_access_token("root")}
recovery = {"Authorization": "Bearer " + create_access_token("root", vendor_recovery=True)}
api = {"X-API-Key": "isolated-key"}
checks = 0


def check(label, got, expected):
    global checks
    assert got == expected, f"{label}: {got!r} != {expected!r}"
    checks += 1
    print("PASS", label)


for scope in licensing.LOCK_SCOPES:
    verdict = licensing.resolve_enforcement(licensing.LicenseStatus(False, licensing.REASON_REVOKED), scope)
    with patch.object(license_guard, "current", return_value=verdict):
        check(scope + " existing JWT blocked", client.get("/api/private", headers=normal).status_code, 403)
        check(scope + " licence status usable", client.get("/api/license/status", headers=normal).status_code, 200)
        check(scope + " key entry usable", client.put("/api/license/key", headers=normal).status_code, 200)
        check(scope + " account status usable", client.get("/api/auth/me", headers=normal).status_code, 200)
        check(scope + " no blanket licence-route exemption", client.post("/api/license/future-admin-route", headers=normal).status_code, 403)
        check(scope + " vendor recovery retained", client.get("/api/private", headers=recovery).status_code, 200)
        expected = 403 if verdict.lock_bot else 200
        check(scope + " remote bot write", client.post("/api/bot/users", headers=api).status_code, expected)
        check(scope + " bot read retained", client.get("/api/bot/users", headers=api).status_code, 200)
        check(scope + " new bot deletion is gated", client.delete("/api/bot/users/customer/purchases/1", headers=api).status_code, expected)
        check(scope + " accepted receipt finalization retained", client.post(
            "/api/accounting/receipt-approvals/11111111-1111-1111-1111-111111111111/finalize", headers=api).status_code, 200)
        with patch.object(miniapp.telegram_webapp, "verify", return_value={"telegram_id": 1, "owner_admin_id": None}):
            check(scope + " Mini App write", client.post("/api/miniapp/checkout").status_code, expected)
            check(scope + " Mini App read retained", client.get("/api/miniapp/home").status_code, 200)

        @bot_auth.bot_route_policy(bot_auth.CUSTOMER_WRITE, "claim")
        def write(*, db, principal):
            return "written"

        for owner in (None, 42):
            with patch.object(panel_bridge, "SessionLocal", Session), patch.object(
                    panel_bridge, "_build_principal", return_value=bot_auth.BotPrincipal.internal(owner, scope_enforced=False)):
                try:
                    result = asyncio.run(panel_bridge._call(write))
                except panel_bridge.ApiError as exc:
                    result = exc.status_code
                check(scope + f" in-process bot owner={owner}", result, 403 if verdict.lock_bot else "written")

locked = licensing.Enforcement(lock_panel=True, lock_bot=True, reason=licensing.REASON_EXPIRED)
with patch.object(license_guard, "current", return_value=locked):
    seller = {"Authorization": "Bearer " + create_access_token("seller", vendor_recovery=True)}
    check("recovery role must still be superadmin", client.get("/api/private", headers=seller).status_code, 403)
    payload = {"sub": "root", "exp": dt.datetime.utcnow() + dt.timedelta(minutes=5), "vendor_recovery": "true"}
    token = jwt.encode(payload, settings.secret_key, algorithm=settings.algorithm)
    check("truthy string is not a recovery claim", client.get("/api/private", headers={"Authorization": "Bearer " + token}).status_code, 403)
    check("invalid API key still unauthorized", client.post("/api/bot/users", headers={"X-API-Key": "bad"}).status_code, 401)

    payload = receipt_approvals.ApprovalIntentIn(
        pending_source_instance_id="test", pending_local_id=1, kind="new", amount=1,
        target_username="test", package_id=1, list_price=1,
    )
    with patch.object(receipt_approvals.registration, "begin") as begin, Session() as db:
        try:
            receipt_approvals.begin_approval(db, bot_auth.BotPrincipal.internal(None, scope_enforced=False), payload, "auto")
        except Exception as exc:
            check("new receipt cannot be registered under bot lock", getattr(exc, "status_code", None), 403)
        else:
            raise AssertionError("registration was not denied")
        check("blocked receipt makes no registration write", begin.call_count, 0)

# TTL refresh is local only, including rollback detection; no heartbeat call.
with tempfile.TemporaryDirectory(prefix="licence-cache-") as folder, patch.object(
        license_state, "_STATE_PATH", str(Path(folder) / "state.json")), patch.object(
        license_state, "_compute", return_value=locked) as compute, patch.object(
        license_state, "heartbeat", side_effect=AssertionError("network must not run")), patch.object(
        license_state.time, "monotonic", return_value=100):
    license_state._cached = licensing.Enforcement()
    license_state._cached_at = 41
    check("59s reuses local verdict", license_state.current_enforcement().lock_panel, False)
    check("cache hit does no recomputation", compute.call_count, 0)
    license_state._cached_at = 40
    check("60s recomputes licence", license_state.current_enforcement().lock_panel, True)
    check("one local recomputation", compute.call_count, 1)

import io


def late_old_key_response(*args, **kwargs):
    settings.license_key = "new-test-key"
    return io.BytesIO(b'{"revoked":true,"lock_scope":"everything"}')


with patch.object(settings, "license_master_install", False), patch.object(settings, "license_key", "old-test-key"), \
        patch.object(settings, "license_server_url", "https://example.invalid"), \
        patch.object(license_state, "_license_id", return_value="old-id"), \
        patch.object(license_state.urllib.request, "urlopen", side_effect=late_old_key_response), \
        patch.object(license_state, "_apply_heartbeat") as accept, patch.object(license_state, "refresh") as refresh:
    license_state.heartbeat()
    check("late response cannot revoke replacement key", accept.call_count, 0)
    check("old response does not refresh successor verdict", refresh.call_count, 0)

print(f"{checks} checks passed")
