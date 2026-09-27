"""REPRODUCTION (2026-09-27 audit, stage 3 - analysis only, no fix here):
models.ApiKey carries no owner/scope at all - any single valid, enabled key
can act as ANY tenant on /api/bot/*, because every scoped endpoint takes
`owner_admin_id` as a caller-supplied query/payload value with nothing
binding it to the key that authenticated the request. This is proven two
ways below:

  1. Over REAL HTTP, through the actual get_bot_api_key dependency (a
     FastAPI TestClient app mounting the real routers.bot.router, with ONE
     ordinary ApiKey row - no different from a third-party integration's
     key, a remote-bot key, or any other) - to show the vulnerability
     survives the real auth layer, not just the inner scoping helpers.
  2. Direct calls into a few endpoints that have NO owner_admin_id
     parameter AT ALL (link_telegram, add_balance, get_payment_card,
     record_payment_card_use) - no amount of "pass the right
     owner_admin_id" caller discipline can fix these; they are unscoped by
     construction, matching test_bot_scope.py's existing pattern for
     calling router functions directly with a real db session.

Nothing here changes production behaviour - see the analysis doc
(docs/api-key-scope-audit-2026-09-27.md) for the endpoint-by-endpoint table
and the proposed identity-bound design, which needs its own approved stage
before any enforcement/migration lands.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import models
from app.database import get_db
from app.routers import bot as bot_router
from app.services import hierarchy
from app.services.keys import generate_api_key

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def add_admin(db, username, *, superadmin=False, role=None):
    row = models.AdminUser(username=username, hashed_password="x", is_superadmin=superadmin, role=role)
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


def add_user(db, username, owner, **extra):
    row = models.User(username=username, owner_admin_id=owner.id if owner else None, **extra)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


# StaticPool - the app under test and this script share one connection, same
# reasoning as test_ip_guard.py's middleware section.
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
db = Session()

admin_a = add_admin(db, "admin_a", role=hierarchy.ROLE_ADMIN)
admin_b = add_admin(db, "admin_b", role=hierarchy.ROLE_ADMIN)
b_customer = add_user(db, "b_customer", admin_b, balance=0, telegram_id=None)

# ONE ordinary key - nothing marks it as "belonging to" admin_a or admin_b.
# This is exactly what a third-party integration's key, a remote-deployed
# bot's key (services/remote_deploy.py), or any other /api/bot/* credential
# looks like today: a bare enabled row with no owner column to check against.
api_key = models.ApiKey(label="third-party integration", key=generate_api_key())
db.add(api_key)
db.commit()

app = FastAPI()
app.include_router(bot_router.router)
app.dependency_overrides[get_db] = lambda: db
client = TestClient(app)
HEADERS = {"X-API-Key": api_key.key}

print("=" * 60)
print("--- REPRODUCTION: one valid key reaches every tenant over real HTTP ---")
print("=" * 60)

print("\n--- the key can create a user under a tenant it has no relation to ---")
resp = client.post("/api/bot/users", json={"username": "a_new_cust", "owner_admin_id": admin_a.id}, headers=HEADERS)
check("create_user succeeds with an arbitrary caller-chosen owner_admin_id", resp.status_code, 200)
check("...and the user really landed under admin_a's tree",
      resp.json()["username"] if resp.status_code == 200 else None, "a_new_cust")
created = db.query(models.User).filter(models.User.username == "a_new_cust").first()
check("...confirmed in the database", created.owner_admin_id if created else None, admin_a.id)

print("\n--- the SAME key reads a DIFFERENT tenant's customer with no scope hint ---")
resp = client.get(f"/api/bot/users/{b_customer.username}", headers=HEADERS)
check("GET /users/{username} with no owner_admin_id sees admin_b's customer too "
      "(the global/unscoped bot behaviour - indistinguishable here from a "
      "malicious or merely careless caller)",
      resp.status_code, 200)

print("\n--- and can mutate that other tenant's customer just as easily ---")
resp = client.post(f"/api/bot/users/{b_customer.username}/set-enabled", params={"enabled": False}, headers=HEADERS)
check("set-enabled on admin_b's own customer succeeds with the same key", resp.status_code, 200)
db.refresh(b_customer)
check("...and actually took effect", b_customer.status, models.UserStatus.disabled)

print("\n--- GET /nodes is not scoped at all - every key sees every enabled node ---")
n1 = models.Node(name="node-a-owned", type=models.NodeType.mikrotik, enabled=True,
                  mt_host="10.0.0.1", mt_username="u", mt_password="p")
db.add(n1)
db.commit()
resp = client.get("/api/bot/nodes", headers=HEADERS)
check("the node list has no owner filter of any kind",
      any(n["name"] == "node-a-owned" for n in resp.json()), True)

print("\n" + "=" * 60)
print("--- REPRODUCTION: some endpoints have NO owner_admin_id parameter at all ---")
print("(not a caller-discipline problem - there is nothing to pass correctly)")
print("=" * 60)

print("\n--- add_balance: no scope parameter exists in the route signature ---")
resp = client.post(
    f"/api/bot/users/{b_customer.username}/add-balance",
    json={"amount": 500000}, headers=HEADERS,
)
check("credits admin_b's customer's wallet with no ownership check whatsoever", resp.status_code, 200)
db.refresh(b_customer)
check("...and the balance really moved", b_customer.balance, 500000)

print("\n--- link_telegram: same - any key can re-link ANY username's telegram_id ---")
resp = client.post(
    f"/api/bot/users/{b_customer.username}/link-telegram",
    json={"telegram_id": 999999}, headers=HEADERS,
)
check("link-telegram on a customer belonging to a completely different "
      "tenant succeeds - hijacking which Telegram account controls that "
      "customer's bot access", resp.status_code, 200)

print("\n--- payment card lookup/bookkeeping: card_id has no ownership check ---")
card = models.PaymentCard(owner_admin_id=admin_b.id, card_number="6037991234567890", card_holder="admin_b's bank card")
db.add(card)
db.commit()
resp = client.get(f"/api/bot/payment-cards/{card.id}", headers=HEADERS)
check("a key with no relation to admin_b can read admin_b's own card details",
      resp.json().get("card_number") if resp.status_code == 200 else None, "6037991234567890")
resp = client.post(f"/api/bot/payment-cards/{card.id}/record-payment", json={"amount": 100000}, headers=HEADERS)
check("...and can also feed fake payment bookkeeping into admin_b's card-rotation state",
      resp.status_code, 200)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
