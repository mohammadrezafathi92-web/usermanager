"""Phase B regression checks for typed API-key creation.

The security boundary in this phase is unusual but intentional: tenant keys
record their future owner/capabilities yet MUST stay disabled until Phase C
enforces those fields centrally. Global and remote-shared-bot keys preserve
today's behavior, while being explicitly classified for that later rollout.
"""
from __future__ import annotations

import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models, schemas
from app.database import get_db
from app.deps import get_current_admin
from app.routers import api_keys, remote_bot
from app.security import hash_password
from app.services import bot_auth


failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


engine = create_engine(
    "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
)
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()

superadmin = models.AdminUser(
    username="root", hashed_password=hash_password("correct-password"), is_superadmin=True
)
owner = models.AdminUser(
    username="tenant-admin", hashed_password=hash_password("x"), is_superadmin=False, role="admin"
)
db.add_all([superadmin, owner])
db.commit()
db.refresh(superadmin)
db.refresh(owner)

app = FastAPI()
app.include_router(api_keys.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_current_admin] = lambda: superadmin
client = TestClient(app)

print("=" * 60)
print("--- tenant integration: classified but unusable until Phase C ---")
print("=" * 60)

resp = client.post(
    "/api/api-keys",
    json={"label": "tenant default", "owner_admin_id": owner.id},
)
check("tenant key creation succeeds", resp.status_code, 200)
tenant_json = resp.json()
check("tenant key is explicitly typed", tenant_json.get("key_type"), bot_auth.KeyType.TENANT_INTEGRATION)
check("tenant key is born disabled", tenant_json.get("enabled"), False)
check("tenant enforcement remains off", tenant_json.get("scope_enforced"), False)
check("tenant owner is stored", tenant_json.get("owner_admin_id"), owner.id)
check("tenant owner name is visible", tenant_json.get("owner_admin_username"), owner.username)
check(
    "tenant default capabilities are the safe two",
    tenant_json.get("capabilities"),
    sorted([bot_auth.CUSTOMER_READ, bot_auth.CUSTOMER_WRITE]),
)

tenant = db.get(models.ApiKey, tenant_json["id"])
check("tenant key records its creator", tenant.created_by_admin_id, superadmin.id)
check("tenant key hash is written immediately", tenant.key_hash, bot_auth.hash_api_key(tenant.key))
check("tenant prefix is written immediately", tenant.key_prefix, tenant.key[:8])
check("tenant last4 is written immediately", tenant.key_last4, tenant.key[-4:])
check("tenant plaintext remains for Phase B compatibility", tenant_json.get("key"), tenant.key)

resp = client.post(f"/api/api-keys/{tenant.id}/toggle")
check("tenant key cannot be enabled before Phase C", resp.status_code, 409)
db.refresh(tenant)
check("failed enable leaves tenant key disabled", tenant.enabled, False)

tenant.enabled = True
db.commit()
resp = client.post(f"/api/api-keys/{tenant.id}/toggle")
check("an accidentally enabled tenant key can still be disabled", resp.status_code, 200)
db.refresh(tenant)
check("tenant safety toggle only moves toward disabled", tenant.enabled, False)

resp = client.post(
    "/api/api-keys",
    json={"label": "empty capability set", "owner_admin_id": owner.id, "capabilities": []},
)
check("an explicit empty capability set is accepted", resp.status_code, 200)
check("explicit empty does not silently become the default", resp.json().get("capabilities"), [])

resp = client.post(
    "/api/api-keys",
    json={"label": "bad capability", "owner_admin_id": owner.id, "capabilities": ["root_everything"]},
)
check("unknown capabilities fail closed at write time", resp.status_code, 400)

resp = client.post(
    "/api/api-keys",
    json={"label": "bad owner", "owner_admin_id": superadmin.id},
)
check("superadmin cannot be used as a fake tenant owner", resp.status_code, 400)

print("\n" + "=" * 60)
print("--- global integration: explicit route + password confirmation ---")
print("=" * 60)

resp = client.post("/api/api-keys/global", json={"label": "global"})
check("global creation challenges for the password", resp.status_code, 403)
resp = client.post(
    "/api/api-keys/global",
    json={"label": "global"},
    headers={"X-Confirm-Password": "wrong"},
)
check("a wrong password cannot create a global key", resp.status_code, 403)
resp = client.post(
    "/api/api-keys/global",
    json={"label": "global"},
    headers={"X-Confirm-Password": "correct-password"},
)
check("correct password creates the global key", resp.status_code, 200)
global_json = resp.json()
check("global key is explicitly typed", global_json.get("key_type"), bot_auth.KeyType.GLOBAL_INTEGRATION)
check("global key has no owner", global_json.get("owner_admin_id"), None)
check("global key is immediately enabled", global_json.get("enabled"), True)
check("global key still has every capability", set(global_json.get("capabilities", [])), set(bot_auth.ALL_CAPABILITIES))
check("global scope enforcement remains off", global_json.get("scope_enforced"), False)

legacy = models.ApiKey(label="legacy", key="legacy-key", key_type=bot_auth.KeyType.LEGACY_GLOBAL, enabled=True)
db.add(legacy)
db.commit()
resp = client.post(f"/api/api-keys/{legacy.id}/toggle")
check("legacy key lifecycle is unchanged", resp.status_code, 200)
db.refresh(legacy)
check("legacy key can still be disabled", legacy.enabled, False)

print("\n" + "=" * 60)
print("--- remote shared bot: classified without behavior change ---")
print("=" * 60)

bot_settings = models.BotSettings(
    id=1, bot_token="telegram-token", admin_ids="123", enabled=True
)
db.add(bot_settings)
db.commit()

real_deploy = remote_bot.remote_deploy.deploy
real_stop = remote_bot.telegram_bot_runner.stop_bot
captured: dict = {}


def fake_deploy(**kwargs):
    captured.update(kwargs)
    return "deployed"


remote_bot.remote_deploy.deploy = fake_deploy
remote_bot.telegram_bot_runner.stop_bot = lambda: None
try:
    result = remote_bot.deploy(
        schemas.RemoteBotDeployRequest(
            host="203.0.113.10",
            ssh_password="one-use-only",
            panel_public_url="https://panel.example.com",
        ),
        db,
        superadmin,
    )
finally:
    remote_bot.remote_deploy.deploy = real_deploy
    remote_bot.telegram_bot_runner.stop_bot = real_stop

db.refresh(bot_settings)
remote_key = db.get(models.ApiKey, bot_settings.remote_api_key_id)
check("remote bot key is explicitly typed", remote_key.key_type, bot_auth.KeyType.REMOTE_SHARED_BOT)
check("remote bot key has no tenant owner", remote_key.owner_admin_id, None)
check("remote bot key remains enabled", remote_key.enabled, True)
check("remote bot scope enforcement remains off", remote_key.scope_enforced, False)
check("remote bot creator is audited", remote_key.created_by_admin_id, superadmin.id)
check("remote bot receives the same plaintext key", captured.get("panel_api_key"), remote_key.key)
check("remote key hash is written", remote_key.key_hash, bot_auth.hash_api_key(remote_key.key))

print("\n" + "=" * 60)
print("--- Phase C C0 boundary: wired in, but every row still unscoped ---")
print("=" * 60)

# This section documented Phase B's OWN boundary ("no BotPrincipal wiring
# yet") - Phase C's C0 stage (docs/api-key-scope-audit-2026-09-27.md,
# نسخه‌ی هشتم) is exactly what wires deps.get_bot_principal, every
# routers/bot.py endpoint, and telegram_bot/panel_bridge.py in, so the old
# "remains free of BotPrincipal" assertions are now testing for the wrong
# thing on purpose - flipped below to confirm the wiring landed, with the
# real "behavior did not change" guarantee covered by
# test_bot_route_policy_coverage.py/test_bot_inprocess_trust_characterization.py
# (every scope_enforced/dedicated_bot_scope_enforced/package_node_scope_enforced
# column still defaults to False) rather than by "the word never appears".

from app import deps
from app.routers import bot
from app.telegram_bot import panel_bridge

check("deps now builds a BotPrincipal", "BotPrincipal" in inspect.getsource(deps), True)
check("bot endpoints now take a BotPrincipal", "BotPrincipal" in inspect.getsource(bot), True)
check("in-process bridge now builds a BotPrincipal", "BotPrincipal" in inspect.getsource(panel_bridge), True)
# _scope() itself must never be removed (see panel_bridge.py's own _scope
# docstring) - wiring principal alongside it, never replacing it.
check("panel_bridge's _scope() defaulting helper is still there", hasattr(panel_bridge, "_scope"), True)

if failures:
    print(f"\n{len(failures)} failure(s): {', '.join(failures)}")
    raise SystemExit(1)
print("\nAll Phase B API-key checks passed.")
