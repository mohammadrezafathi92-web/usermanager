"""Panel bulk renewal through the durable path (Lifecycle 7.5): N
independent renew_purchase operations, no batch charge, no batch refund.

  * with a package: every service's limits REPLACED by the package (today's
    bulk semantics), counted as a purchase; the acting reseller pays the
    package cost once per service, each captured with its own sale;
  * a member the reseller cannot afford: refused alone - the others are
    complete, nothing of it was renewed, charged or booked;
  * raw gigabytes at the reseller's per-GB rate: renewal at that rate each;
  * a status change in the same request still applies to every customer;
  * type 'legacy': today's batch code runs.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-bulk-renew-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import users as users_router
from app.services import provisioning_durable as durable, provisioning_schema, receipt_void_schema
from app.services import user_ops, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []
GB = 1024 ** 3


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("b9" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    reseller = models.AdminUser(username="res", hashed_password="x", balance=650, wholesale_price_per_gb=0)
    db.add(reseller)
    db.flush()
    package = models.Package(name="P20", quota_gb=20, duration_days=60, price=400, cooperation_price=300,
                             owner_admin_id=reseller.id)
    db.add(package)
    db.flush()
    targets = []
    for index in (1, 2, 3):
        user = wallet_accounts.create_user_with_wallet(db, username=f"c{index}", telegram_id=900 + index,
                                                       owner_admin_id=reseller.id)
        db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=GB, used_bytes=GB,
                                   expire_at=dt.datetime.utcnow() + dt.timedelta(days=1),
                                   status=models.UserStatus.quota_exceeded)
        db.add(purchase)
        db.flush()
        targets.append((user.id, purchase.id))
    db.commit()
    RESELLER, PACKAGE = reseller.id, package.id
USERS = [u for u, _ in targets]
NO_RECONCILE = patch.object(user_ops, "reconcile_purchase_connections", return_value=None)


def set_mode(mode):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, "renew_purchase").mode = mode
        db.commit()


def bulk(**changes):
    with SessionLocal() as db, NO_RECONCILE:
        admin = db.get(models.AdminUser, RESELLER)
        return users_router.bulk_update_users(schemas.BulkUpdateUsersRequest(user_ids=USERS, **changes), db=db,
                                              admin=admin, _perm=None, x_idempotency_key=str(uuid.uuid4()))


def services():
    with SessionLocal() as db:
        return [(p.quota_bytes, p.used_bytes, p.status.value) for p in
                db.query(models.Purchase).order_by(models.Purchase.id)]


def balance():
    with SessionLocal() as db:
        return db.get(models.AdminUser, RESELLER).balance


def ledger(kind):
    with SessionLocal() as db:
        return db.query(models.LedgerEntry).filter_by(kind=kind).count()


# ------------------------------------------------------------------ legacy
set_mode("legacy")
with SessionLocal() as db:
    db.get(models.AdminUser, RESELLER).balance = 5000
    db.commit()
with patch.object(user_ops, "bulk_update_users", side_effect=RuntimeError("legacy path used")):
    try:
        bulk(package_id=PACKAGE)
        legacy = "durable"
    except RuntimeError as exc:
        legacy = str(exc)
check("type 'legacy': today's batch code runs", legacy, "legacy path used")
with SessionLocal() as db:                      # the legacy try/except refunded its batch charge
    db.get(models.AdminUser, RESELLER).balance = 650
    db.commit()

# ------------------------------------------------------------------ durable, package, one unaffordable member
set_mode("durable")
spend_before, refund_before = ledger("admin_credit_spend"), ledger("admin_credit_refund")
result = bulk(package_id=PACKAGE)
with SessionLocal() as db:
    ops = db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.business_key.like("bulk_renew:%")).all()
check("independent operations: two complete; the third refused alone in its own T1 (credit 650 covers two)",
      (len(ops), sorted(op.state for op in ops), result["updated_count"], len(result["failed"])),
      (2, ["completed", "completed"], 2, 1))
check("the two renewed services' limits replaced by the package; the third untouched",
      services(), [(20 * GB, GB, "active"), (20 * GB, GB, "active"), (GB, GB, "quota_exceeded")])
check("the reseller paid exactly two package costs; two sales; no batch charge or refund",
      (650 - balance(), ledger("sale_renew"), ledger("admin_credit_spend") - spend_before,
       ledger("admin_credit_refund") - refund_before), (600, 2, 2, 0))
with SessionLocal() as db:
    counts = [db.get(models.User, u).purchase_count or 0 for u in USERS]
check("each renewed customer counted one purchase (today's bulk semantics)", counts, [1, 1, 0])

# ------------------------------------------------------------------ raw gigabytes at a rate + status
with SessionLocal() as db:
    admin = db.get(models.AdminUser, RESELLER)
    admin.balance, admin.wholesale_price_per_gb = 1000, 10
    db.commit()
with patch.object(users_router.admin_billing, "require_package_to_grant", return_value=None):
    result = bulk(add_gb=5, status=models.UserStatus.disabled)
check("raw 5 GB at 10 per GB: three renewals, 50 each", (result["updated_count"], 1000 - balance()), (3, 150))
with SessionLocal() as db:
    check("the status change in the same request applied to every customer",
          [db.get(models.User, u).status.value for u in USERS], ["disabled"] * 3)

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
