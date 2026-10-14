"""L5 wiring: renewals through the durable path from the REAL endpoints
(Lifecycle 7.4, 8, 16). No node is contacted (no connection to re-enable).

  * bot renew_service, durable: renewal + reseller reservation/capture + sale
    in ONE transaction; the reseller is debited exactly what _charge_seller
    would debit; one admin_credit_spend and one sale_renew row; today's
    response; the same X-Idempotency-Key again changes nothing;
  * a reseller without enough credit: refused, nothing renewed or booked;
  * panel renew: the ACTING admin pays (charge_for_renewal), the panel sale
    is booked with its note; a raw-gigabyte renewal at a per-GB rate is a
    cost without a sale;
  * panel usage reset sold as a package keeps the service's own package;
  * type 'legacy': today's code runs.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-renew-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, users as users_router
from app.services import bot_auth, provisioning_durable as durable, provisioning_schema, receipt_void_schema
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
HOST = HostIdentity("e5" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=1000)
    db.add_all([root, reseller])
    db.flush()
    package = models.Package(name="P30", quota_gb=10, duration_days=30, price=300, cooperation_price=200,
                             owner_admin_id=reseller.id)
    db.add(package)
    db.flush()
    user = wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=88, owner_admin_id=reseller.id)
    db.flush()
    service = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=10 * GB, used_bytes=10 * GB,
                              expire_at=dt.datetime.utcnow() + dt.timedelta(days=3),
                              status=models.UserStatus.quota_exceeded)
    db.add(service)
    db.commit()
    USER, SERVICE, RESELLER, PACKAGE, ROOT = user.id, service.id, reseller.id, package.id, root.id
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)


def set_mode(mode):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, "renew_purchase").mode = mode
        db.commit()


def balance(admin_id):
    with SessionLocal() as db:
        return db.get(models.AdminUser, admin_id).balance


def ledger():
    with SessionLocal() as db:
        return sorted((row.kind, row.amount, row.note) for row in db.query(models.LedgerEntry))


def bot_renew(key, paid=250):
    with SessionLocal() as db:
        payload = schemas.BotRenewRequest(package_id=PACKAGE, add_gb=10, add_days=30, paid_amount=paid,
                                          payment_method="card")
        return bot_router.renew_service("cust", SERVICE, payload, db=db, owner_admin_id=None, principal=PRINCIPAL,
                                        x_idempotency_key=key)


# ------------------------------------------------------------------ legacy
with patch.object(user_ops, "renew_purchase", side_effect=RuntimeError("legacy path used")):
    try:
        bot_renew(str(uuid.uuid4()))
        legacy = "durable path"
    except RuntimeError as exc:
        legacy = str(exc)
check("type 'legacy': bot renew_service runs today's code", legacy, "legacy path used")

# ------------------------------------------------------------------ bot durable
set_mode("durable")
key = str(uuid.uuid4())
before = balance(RESELLER)
out = bot_renew(key)
with SessionLocal() as db:
    purchase = db.get(models.Purchase, SERVICE)
    operation = db.query(mp.ProvisioningOperation).filter_by(operation_type="renew_purchase").one()
    reservation = db.query(mp.PaymentReservation).filter_by(operation_id=operation.id).one()
check("bot durable renewal: one completed operation, the exhausted service restarted, reservation captured",
      (operation.state, purchase.used_bytes, purchase.quota_bytes, reservation.state, reservation.amount),
      ("completed", 0, 10 * GB, "captured", 200))
check("the reseller paid exactly the cooperation price, once", before - balance(RESELLER), 200)
check("one sale_renew (the paid amount) and one admin_credit_spend", [(k, a) for k, a, _ in ledger()],
      [("admin_credit_spend", 200), ("sale_renew", 250)])
check("today's response body", (type(out).__name__, out.username), ("BotUserResponse", "cust"))
again = bot_renew(key)
check("the same key again: nothing more charged or booked", (balance(RESELLER), len(ledger())), (before - 200, 2))

# ------------------------------------------------------------------ insufficient credit
with SessionLocal() as db:
    db.get(models.AdminUser, RESELLER).balance = 50
    db.commit()
try:
    bot_renew(str(uuid.uuid4()))
    poor = "renewed"
except HTTPException as exc:
    poor = exc.status_code
check("not enough reseller credit: refused, nothing renewed, booked or debited",
      (poor, balance(RESELLER), len(ledger())), (400, 50, 2))
with SessionLocal() as db:
    db.get(models.AdminUser, RESELLER).balance = 1000
    db.commit()

# ------------------------------------------------------------------ panel renew (acting admin pays)
with SessionLocal() as db:
    acting = db.get(models.AdminUser, RESELLER)
    acting.wholesale_price_per_gb = 7
    db.commit()
with SessionLocal() as db:
    acting = db.get(models.AdminUser, RESELLER)
    with patch.object(users_router.admin_billing, "require_package_to_grant", return_value=None):
        users_router.renew_purchase_endpoint(USER, SERVICE, schemas.PurchaseRenewRequest(add_gb=5, add_days=0), db=db,
                                             admin=acting, _perm=None, x_idempotency_key=str(uuid.uuid4()))
check("panel raw-gigabyte renewal at a per-GB rate: the acting admin pays 5 x 7, no sale",
      (balance(RESELLER), [(k, a) for k, a, _ in ledger()].count(("admin_credit_spend", 35)), len(ledger())),
      (1000 - 35, 1, 3))
with SessionLocal() as db:
    acting = db.get(models.AdminUser, RESELLER)
    acting.wholesale_price_per_gb = 0
    db.commit()
with SessionLocal() as db:
    acting = db.get(models.AdminUser, RESELLER)
    users_router.renew_purchase_endpoint(USER, SERVICE, schemas.PurchaseRenewRequest(package_id=PACKAGE), db=db,
                                         admin=acting, _perm=None, x_idempotency_key=str(uuid.uuid4()))
check("panel package renewal: the panel sale is booked with its note", any(
    k == "sale_renew" and n == "فروش از پنل" for k, _, n in ledger()), True)

# ------------------------------------------------------------------ panel reset sold as a package keeps the package
with SessionLocal() as db:
    other = models.Package(name="OTHER", quota_gb=1, duration_days=1, price=10, cooperation_price=10, owner_admin_id=RESELLER)
    db.add(other)
    db.commit()
    other_id = other.id
with SessionLocal() as db:
    acting = db.get(models.AdminUser, RESELLER)
    users_router.reset_purchase_usage(USER, SERVICE, schemas.ResetUsageRequest(package_id=other_id), db=db,
                                      admin=acting, _perm=None)
with SessionLocal() as db:
    check("panel usage reset priced by another package: usage zeroed, the service keeps its package",
          (db.get(models.Purchase, SERVICE).used_bytes, db.get(models.Purchase, SERVICE).package_id), (0, PACKAGE))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
