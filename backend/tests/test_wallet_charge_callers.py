"""Lifecycle 8: a wallet sale is charged by the panel, in the same operation
as the service (charge_wallet) - never debit-first with a best-effort refund
by the caller. Fake runner; no network.

  * durable purchase_package with charge_wallet: the customer's wallet is
    reserved in T1 and captured in T_final - charged exactly once;
  * durable failure: the reservation is released, the wallet unchanged, no
    service; not enough balance: refused, nothing changes;
  * durable wallet sale WITHOUT charge_wallet (an old caller that already
    debited): refused before anything (no double charge);
  * durable renew_service with charge_wallet: one charge, one renewal;
  * legacy type: the endpoint itself debits, provisions, and refunds a
    failure (the caller never holds the money);
  * the Mini App checkout and the bot's pay_with_balance no longer call
    add_balance themselves.
"""
import ast
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-wallet-charge-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, miniapp
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import remote_action as action, user_ops, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("a7" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    db.add(root)
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.13.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.31.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    package = models.Package(name="One", quota_gb=10, duration_days=30, price=400)
    db.add(package)
    db.flush()
    db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
    customer = wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=700, balance=1000)
    db.commit()
    PACKAGE, CUSTOMER, NODE = package.id, customer.id, node.id
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))


def set_mode(kind, mode):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = mode
        db.commit()


def wallet():
    with SessionLocal() as db:
        return db.get(models.User, CUSTOMER).balance


def services():
    with SessionLocal() as db:
        return db.query(models.Purchase).filter_by(user_id=CUSTOMER).count()


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def buy(runner=ok_runner, charge=True, price=400):
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        try:
            bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
                package_id=PACKAGE, paid_amount=price, payment_method="wallet", charge_wallet=charge),
                db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=str(uuid.uuid4()))
            return "ok"
        except HTTPException as exc:
            return exc.status_code


def last_reservation():
    with SessionLocal() as db:
        operation = db.query(mp.ProvisioningOperation).order_by(mp.ProvisioningOperation.id.desc()).first()
        rows = db.query(mp.PaymentReservation).filter_by(operation_id=operation.id).all()
        return operation.state, [(row.payer_kind, row.amount, row.state) for row in rows]


# ------------------------------------------------------------------ durable
set_mode("purchase", "durable")
set_mode("renew_purchase", "durable")
check("durable purchase with charge_wallet: done, the wallet charged once", (buy(), wallet(), services()), ("ok", 600, 1))
check("...the wallet reserved in T1 and captured with the service", last_reservation(),
      ("completed", [("customer_wallet", 400, "captured")]))


def failing(dto):
    if dto.action_type is not action.ActionType.WG_ENSURE_PRESENT:
        return ok_runner(dto)
    with SessionLocal() as db:
        db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.state.notin_(
            ("completed", "compensated"))).update({"forward_deadline": dt.datetime(2000, 1, 1)})
        db.commit()
    return action.RemoteActionResult(dto.action_id, action.Outcome.TRANSPORT_ERROR, write_attempted=False,
                                     error_code="connect_failed")


check("durable failure: refused, the wallet unchanged, no service", (buy(failing), wallet(), services()), (400, 600, 1))
check("...the wallet reservation was released", last_reservation(),
      ("compensated", [("customer_wallet", 400, "released")]))
check("not enough balance: refused, nothing changes", (buy(price=5000), wallet(), services()), (400, 600, 1))
before_ops = None
with SessionLocal() as db:
    before_ops = db.query(mp.ProvisioningOperation).count()
check("a wallet sale WITHOUT charge_wallet (the caller debited elsewhere): refused, no double charge",
      (buy(charge=False), wallet(), services()), (422, 600, 1))
with SessionLocal() as db:
    check("...refused before any operation was written", db.query(mp.ProvisioningOperation).count(), before_ops)
    purchase_id = db.query(models.Purchase).filter_by(user_id=CUSTOMER).one().id
with SessionLocal() as db:
    try:
        bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
            package_id=PACKAGE, paid_amount=400, payment_method="card", charge_wallet=True),
            db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=str(uuid.uuid4()))
        invalid = "ok"
    except HTTPException as exc:
        invalid = exc.detail
check("charge_wallet on a non-wallet sale is invalid", invalid, "wallet_charge_invalid")
with SessionLocal() as db, NO_SHARE, patch.object(user_ops, "reconcile_purchase_connections", return_value=None):
    bot_router.renew_service("cust", purchase_id, schemas.BotRenewRequest(
        package_id=PACKAGE, add_gb=10, add_days=30, paid_amount=300, payment_method="wallet", charge_wallet=True),
        db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=str(uuid.uuid4()))
check("durable renew_service with charge_wallet: one charge", (wallet(), last_reservation()),
      (300, ("completed", [("customer_wallet", 300, "captured")])))

# ------------------------------------------------------------------ the Mini App checkout
with SessionLocal() as db:
    db.get(models.User, CUSTOMER).balance = 1000
    db.commit()
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner), \
        patch.object(miniapp, "_package_or_404", side_effect=lambda d, o, i: d.get(models.Package, i)), \
        patch.object(miniapp, "_notify_owner_of_sale", return_value=None), \
        patch.object(bot_router, "add_balance", side_effect=AssertionError("the checkout debited itself")):
    out = miniapp.checkout(miniapp.CheckoutRequest(package_id=PACKAGE), visitor={
        "owner_admin_id": None, "telegram_id": 700, "user": {}}, db=db)
check("Mini App checkout (durable): done through charge_wallet, charged once",
      (out["status"], wallet(), last_reservation()[1]), ("done", 600, [("customer_wallet", 400, "captured")]))

# ------------------------------------------------------------------ legacy
set_mode("purchase", "legacy")
before = services()


def legacy_purchase(db, user, package, **kwargs):     # today's provisioning, without a node
    purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=1, status=models.UserStatus.active)
    db.add(purchase)
    db.commit()
    return purchase


with patch.object(user_ops, "apply_package_as_purchase", side_effect=legacy_purchase):
    check("legacy purchase with charge_wallet: the endpoint debits", (buy(), wallet(), services()),
          ("ok", 200, before + 1))
with patch.object(user_ops, "apply_package_as_purchase", side_effect=HTTPException(400, "node down")):
    check("legacy failure: the endpoint refunds, nothing remains", (buy(), wallet(), services()),
          (400, 200, before + 1))
check("legacy, not enough balance: refused before provisioning", (buy(price=5000), wallet()), (400, 200))

# ------------------------------------------------------------------ the callers no longer debit
for path, function in (("app/routers/miniapp.py", "checkout"),
                       ("app/telegram_bot/handlers/customer_purchase.py", "pay_with_balance")):
    tree = ast.parse(open(os.path.join(BACKEND, path), encoding="utf-8").read())
    body = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == function)
    debits = [node for node in ast.walk(body) if isinstance(node, ast.Call)
              and getattr(node.func, "attr", getattr(node.func, "id", None)) == "add_balance"]
    check(f"{path}:{function} never calls add_balance", debits, [])

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
