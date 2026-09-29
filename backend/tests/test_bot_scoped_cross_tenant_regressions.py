"""Phase C code review (round after C0's first commit batch) found three
real cross-tenant defects the C0 wiring itself introduced or left open,
each reproduced against real code before being fixed - not caught by
test_bot_auth_principal.py (which tests the guard FUNCTIONS in isolation),
test_bot_route_policy_coverage.py (which only proves every route HAS a
capability decorator, not that its resource check actually works), or
test_bot_api_key_cross_tenant.py (whose whole point is documenting the
UNSCOPED case, still correct and unchanged by any of this - see its own
docstring).

Every check below uses a SCOPED principal (scope_enforced=True) - exactly
what C1+ activates a key/dedicated bot into, and exactly the case none of
these three defects were ever exercised under before this file. All three
were confirmed BROKEN against the C0 commit batch, then confirmed FIXED
here:

  1. bot_resources._get_user_or_403 had silently dropped the
     claimed_owner_admin_id parameter the old (pre-Phase-C) _get_user_or_404
     took - since every dedicated bot is unscoped by default
     (dedicated_bot_scope_enforced=False), and require_bot_user_access is a
     no-op for an unscoped principal, this meant Admin A's own dedicated
     bot could read/mutate Admin B's customers by username, something the
     OLD code never allowed (its owner_admin_id-based check ran
     UNCONDITIONALLY whenever a caller passed one, independent of any
     scope_enforced concept). Fixed by giving the accessor a mandatory
     claimed_owner_admin_id parameter and restoring the unconditional
     hierarchy check on top of require_bot_user_access.
  2. create_user/purchase_package/renew_service/renew loaded payload.
     package_id via a raw db.get(), bypassing bot_resources.
     _get_package_or_403 entirely - a scoped tenant could buy/renew into
     another tenant's private package (and, for a bundled package, ride
     its owner's own node authorization). Fixed by routing every one of
     those four lookups through the accessor.
  3. _record_bot_sale (every bot sale endpoint's shared accounting hook)
     and add_balance's wallet-topup branch both fed payload.
     payment_card_id straight into accounting.record with no ownership
     check - a scoped tenant's sale/top-up could be permanently attributed
     to a foreign reseller's card, corrupting that reseller's own
     rotation/aggregate totals. Fixed by validating the card via
     _get_payment_card_or_403 before either accounting.record call.

A fourth finding (test_phase_c_lifecycle.py's original concurrency test
proved two activation calls don't collide with each other, not the actual
package-write-vs-activation invariant the lock exists for) was a test-only
defect, fixed in that file directly rather than here.

Run:  python3 backend/tests/test_bot_scoped_cross_tenant_regressions.py
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
from app.deps import get_bot_principal
from app.routers import bot as bot_router
from app.security import hash_password
from app.services import hierarchy
from app.services.bot_auth import BotPrincipal

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def add_admin(db, username, *, balance=0):
    row = models.AdminUser(username=username, hashed_password=hash_password("x"), role="admin", balance=balance)
    db.add(row)
    db.commit()
    db.refresh(row)
    hierarchy.rebuild_path(db, row)
    db.commit()
    return row


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()

admin_a = add_admin(db, "admin_a", balance=1_000_000)
admin_b = add_admin(db, "admin_b")

cust_a = models.User(username="cust_a", owner_admin_id=admin_a.id)
cust_b = models.User(username="cust_b_secret", owner_admin_id=admin_b.id)
pkg_a = models.Package(name="a-pkg", quota_gb=1, duration_days=30, price=0,
                       enabled=True, bot_enabled=True, owner_admin_id=admin_a.id)
pkg_b = models.Package(name="b-private", quota_gb=1, duration_days=30, price=0,
                       enabled=True, bot_enabled=True, owner_admin_id=admin_b.id)
card_a = models.PaymentCard(owner_admin_id=admin_a.id, card_number="6037991111111111", card_holder="admin_a card")
card_b = models.PaymentCard(owner_admin_id=admin_b.id, card_number="6037992222222222", card_holder="admin_b card")
db.add_all([cust_a, cust_b, pkg_a, pkg_b, card_a, card_b])
db.commit()

# A key/dedicated bot scoped strictly to admin_a - exactly what a Phase C
# activation (C1+, out of scope for THIS approval) eventually turns on.
principal_a = BotPrincipal.internal(admin_a.id, scope_enforced=True)

app = FastAPI()
app.include_router(bot_router.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[get_bot_principal] = lambda: principal_a
client = TestClient(app)


print("=" * 72)
print("--- [1] _get_user_or_403: a scoped dedicated bot cannot reach another tenant's customer by name ---")
print("=" * 72)

resp = client.get(f"/api/bot/users/{cust_b.username}")
check("GET /users/{username} for a foreign tenant's customer is refused (was a 200 leak before the fix)",
      resp.status_code, 404)

resp = client.get(f"/api/bot/users/{cust_a.username}")
check("...but its OWN tenant's customer is still reachable, unaffected",
      resp.status_code, 200)

resp = client.post(f"/api/bot/users/{cust_b.username}/set-enabled", params={"enabled": False})
check("mutating a foreign tenant's customer is refused the same way",
      resp.status_code, 404)
db.refresh(cust_b)
check("...and nothing actually changed", cust_b.status, models.UserStatus.active)


print("\n" + "=" * 72)
print("--- [2] package_id: a scoped tenant cannot buy/renew into another tenant's private package ---")
print("=" * 72)

before_purchases = db.query(models.Purchase).count()
resp = client.post(
    f"/api/bot/users/{cust_a.username}/purchase-package",
    json={"package_id": pkg_b.id},
)
check("purchase-package against a foreign tenant's private package is refused "
      "(previously a raw db.get() with no ownership check at all)",
      resp.status_code, 404)
check("...and no Purchase row was created for the refused attempt - review round 2 "
      "caught that _get_package_or_403 alone (without also checking payment_card_id "
      "early, see [3] below) does not by itself guarantee this for every endpoint",
      db.query(models.Purchase).count(), before_purchases)

resp = client.post(
    f"/api/bot/users/{cust_a.username}/purchase-package",
    json={"package_id": pkg_a.id},
)
check("...but its own tenant's package still purchases fine", resp.status_code, 200)
cust_a_purchase = (
    db.query(models.Purchase)
    .filter(models.Purchase.user_id == cust_a.id)
    .order_by(models.Purchase.id.desc())
    .first()
)
before_reserved_pkg = cust_a_purchase.reserved_package_id
before_pkg_id = cust_a_purchase.package_id

print("\n--- [2b] same defect, the renew paths: renew_purchase/renew_user commit BEFORE package_id was checked ---")

resp = client.post(
    f"/api/bot/users/{cust_a.username}/purchases/{cust_a_purchase.id}/renew",
    json={"add_gb": 1, "package_id": pkg_b.id},
)
check("renew-purchase against a foreign tenant's private package is refused "
      "(review round 2: this endpoint calls user_ops.renew_purchase - which commits "
      "internally - BEFORE it used to check package_id at all)",
      resp.status_code, 404)
db.refresh(cust_a_purchase)
check("...and the Purchase's reserved_package_id was never stamped with the foreign id "
      "(this is the actual regression review round 2 found: a 404 response that still "
      "left the row durably mutated)",
      cust_a_purchase.reserved_package_id, before_reserved_pkg)
check("...nor was its own package_id touched either",
      cust_a_purchase.package_id, before_pkg_id)


print("\n" + "=" * 72)
print("--- [3] payment_card_id: a scoped tenant's sale/top-up cannot be attributed to a foreign card ---")
print("=" * 72)

cust_a2 = models.User(username="cust_a2", owner_admin_id=admin_a.id)
db.add(cust_a2)
db.commit()

before_entries = db.query(models.LedgerEntry).count()
before_purchases = db.query(models.Purchase).count()
resp = client.post(
    f"/api/bot/users/{cust_a2.username}/purchase-package",
    json={"package_id": pkg_a.id, "paid_amount": 1000, "payment_card_id": card_b.id},
)
check("a purchase whose payment_card_id belongs to a FOREIGN tenant is refused entirely, "
      "not silently recorded against the wrong card",
      resp.status_code, 404)
check("...and no ledger row was written for it at all", db.query(models.LedgerEntry).count(), before_entries)
check("...and no Purchase row was created either - review round 2's actual finding: "
      "apply_package_as_purchase commits internally BEFORE the old code checked "
      "payment_card_id at all, so the customer got a real service despite the 404",
      db.query(models.Purchase).count(), before_purchases)

resp = client.post(
    f"/api/bot/users/{cust_a.username}/purchase-package",
    json={"package_id": pkg_a.id, "paid_amount": 1000, "payment_card_id": card_a.id},
)
check("...but attributing the sale to its OWN tenant's card succeeds",
      resp.status_code, 200)
entry = (
    db.query(models.LedgerEntry)
    .filter(models.LedgerEntry.payment_card_id == card_a.id)
    .order_by(models.LedgerEntry.id.desc())
    .first()
)
check("...and the ledger row really does point at the right card",
      entry.payment_card_id if entry else None, card_a.id)

before_entries = db.query(models.LedgerEntry).count()
resp = client.post(
    f"/api/bot/users/{cust_a.username}/add-balance",
    json={"amount": 5000, "payment_card_id": card_b.id},
)
check("add_balance's own wallet-topup ledger write has the SAME check - a foreign "
      "card_id is refused there too, not just in _record_bot_sale",
      resp.status_code, 404)
check("...and no top-up ledger row was written either", db.query(models.LedgerEntry).count(), before_entries)


print("\n" + "=" * 72)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
