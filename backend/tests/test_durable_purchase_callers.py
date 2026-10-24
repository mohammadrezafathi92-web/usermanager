"""L6/L7 wiring: purchase and create_user through the durable path from the
REAL endpoints (Lifecycle 1, 7.1-7.5, 8, 11.5). Fake runner; no network.

  * bot purchase_package: every connection of the service or none; the
    reseller's credit reserved in T1, captured with the sale; the sale basis
    for a later reseller refund bound in the same commit;
  * a failure on the SECOND connection after the first was created: the
    first is removed by identity, nothing remains (no Purchase, no
    Connection, no sale), the credit is released - all-or-nothing;
  * panel apply_package: the acting admin pays; panel create_user with a
    package: user + service + connections in one operation;
  * bot create_user: created_via="bot"; a quota/expiry that differs from the
    frozen package is refused (409);
  * panel bulk create: N independent operations; a taken name is skipped;
    each member reserves its own credit;
  * a username held by an unfinished durable create cannot be taken by the
    legacy creation path.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-purchase-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.models_reseller_refund import ResellerSaleBasis
from app.routers import bot as bot_router, users as users_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import remote_action as action, user_ops, wallet_accounts
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
HOST = HostIdentity("f6" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    runtime = db.get(mp.ProvisioningRuntimeState, 1)
    runtime.owner_state, runtime.owner_host_id, runtime.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    runtime.owner_claimed_at = runtime.owner_heartbeat_at = runtime.lock_verified_at = dt.datetime.utcnow()
    runtime.ownership_epoch, runtime.gate_mode, runtime.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=10_000)
    db.add_all([root, reseller])
    db.flush()
    nodes = [models.Node(name=f"mt{i}", type=models.NodeType.mikrotik, enabled=True, mt_host=f"10.12.0.{i}",
                         mt_wireguard_interface="wg0", mt_client_subnet=f"10.{20 + i}.0.0/24") for i in (1, 2)]
    db.add_all(nodes)
    db.flush()
    for node in nodes:
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
            verified_at=dt.datetime.utcnow()))
        db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
    package = models.Package(name="Duo", quota_gb=20, duration_days=30, price=500, cooperation_price=300,
                             owner_admin_id=reseller.id)
    db.add(package)
    db.flush()
    for node in nodes:
        db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
    customer = wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=600, owner_admin_id=reseller.id)
    db.commit()
    RESELLER, PACKAGE, CUSTOMER, N1, N2 = reseller.id, package.id, customer.id, nodes[0].id, nodes[1].id
for kind in ("purchase", "create_user"):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))


def balance():
    with SessionLocal() as db:
        return db.get(models.AdminUser, RESELLER).balance


def counts():
    with SessionLocal() as db:
        return (db.query(models.Purchase).count(), db.query(models.Connection).count(),
                db.query(models.LedgerEntry).filter_by(kind="sale_new").count())


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


# ------------------------------------------------------------------ bot purchase_package: all
before, start = balance(), counts()
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    out = bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(package_id=PACKAGE, paid_amount=500,
                                      payment_method="card"), db=db, owner_admin_id=None, principal=PRINCIPAL,
                                      x_idempotency_key=str(uuid.uuid4()))
with SessionLocal() as db:
    purchase = db.query(models.Purchase).filter_by(user_id=CUSTOMER).one()
    basis = db.query(ResellerSaleBasis).filter_by(purchase_id=purchase.id).one_or_none()
    check("bot purchase: one service with BOTH connections, the reseller paid the cooperation price, one sale",
          (len(out.connections), len(purchase.connections), before - balance(), counts()[2] - start[2]),
          (2, 2, 300, 1))
    check("the reseller-refund sale basis is bound to the debit in the same commit",
          (basis is not None, basis.charged_amount if basis else None, basis.purchase_count_delta if basis else None),
          (True, 300, 1))

# ------------------------------------------------------------------ failure on the second connection
calls = []


def second_fails(dto):
    calls.append(dto.action_type)
    if dto.action_type is action.ActionType.WG_ENSURE_PRESENT and dto.node_id == N2:
        with SessionLocal() as db:            # the operation can no longer roll forward
            db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.state.notin_(
                ("completed", "compensated"))).update({"forward_deadline": dt.datetime(2000, 1, 1)})
            db.commit()
        return action.RemoteActionResult(dto.action_id, action.Outcome.TRANSPORT_ERROR, write_attempted=False,
                                         error_code="connect_failed")
    return ok_runner(dto)


before, start = balance(), counts()
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=second_fails):
    try:
        bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(package_id=PACKAGE, paid_amount=500,
                                    payment_method="card"), db=db, owner_admin_id=None, principal=PRINCIPAL,
                                    x_idempotency_key=str(uuid.uuid4()))
        failed = "completed"
    except HTTPException as exc:
        failed = exc.status_code
with SessionLocal() as db:
    operation = db.query(mp.ProvisioningOperation).order_by(mp.ProvisioningOperation.id.desc()).first()
    reservation = db.query(mp.PaymentReservation).filter_by(operation_id=operation.id).one()
check("second connection fails: the request fails, the operation is compensated",
      (failed, operation.state), (400, "compensated"))
check("all-or-nothing: the first connection was removed by identity, nothing remains, credit released",
      (action.ActionType.WG_ENSURE_ABSENT in calls, counts(), balance(), reservation.state),
      (True, start, before, "released"))

# ------------------------------------------------------------------ panel apply_package and create_user
with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    acting = db.get(models.AdminUser, RESELLER)
    before = balance()
    users_router.apply_package(CUSTOMER, schemas.ApplyPackageRequest(package_id=PACKAGE), db=db, admin=acting,
                               _perm=None, x_idempotency_key=str(uuid.uuid4()))
    check("panel apply_package: the acting admin paid, a second complete service exists",
          (before - balance(), db.query(models.Purchase).filter_by(user_id=CUSTOMER).count()), (300, 2))
with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    acting = db.get(models.AdminUser, RESELLER)
    created = users_router.create_user(schemas.UserCreate(username="paneluser", package_id=PACKAGE, full_name="F"),
                                       db=db, admin=acting, _perm=None, x_idempotency_key=str(uuid.uuid4()))
    fresh = db.query(models.User).filter_by(username="paneluser").one()
    check("panel create_user with a package: user + one service + both connections, one operation",
          (created.username, fresh.full_name, len(fresh.purchases), len(fresh.connections)), ("paneluser", "F", 1, 2))

# ------------------------------------------------------------------ bot create_user
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    request = schemas.BotCreateUserRequest(username="botuser", telegram_id=601, owner_admin_id=RESELLER,
                                           package_id=PACKAGE, quota_gb=20, expire_days=30, paid_amount=500,
                                           connections=[schemas.BotCreateConnectionSpec(node_id=N1, protocol="wireguard")])
    bot_router.create_user(request, db=db, principal=PRINCIPAL, x_idempotency_key=str(uuid.uuid4()))
    user = db.query(models.User).filter_by(username="botuser").one()
    check("bot create_user: created_via bot, its own service and connection",
          (user.created_via, len(user.purchases), len(user.connections)), ("bot", 1, 1))
    mismatch = schemas.BotCreateUserRequest(username="botuser2", telegram_id=602, owner_admin_id=RESELLER,
                                            package_id=PACKAGE, quota_gb=5, expire_days=30)
    try:
        bot_router.create_user(mismatch, db=db, principal=PRINCIPAL, x_idempotency_key=str(uuid.uuid4()))
        refused = "created"
    except HTTPException as exc:
        refused = exc.detail
check("bot create_user with limits different from the frozen package: refused", refused,
      "provisioning_package_limits_mismatch")

# ------------------------------------------------------------------ bulk create
with SessionLocal() as db:
    wallet_accounts.create_user_with_wallet(db, username="bulk2", owner_admin_id=RESELLER)
    db.commit()
before = balance()
with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    acting = db.get(models.AdminUser, RESELLER)
    result = users_router.bulk_create_users(schemas.BulkCreateUsersRequest(prefix="bulk", count=3, package_id=PACKAGE),
                                            db=db, admin=acting, _perm=None, x_idempotency_key=str(uuid.uuid4()))
with SessionLocal() as db:
    ops = db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.business_key.like("bulk:%")).count()
check("bulk create: 3 independent operations, the taken name skipped, each member charged once",
      (result["created"], [s["name"] for s in result["skipped"]], ops, before - balance()),
      (["bulk1", "bulk3", "bulk4"], ["bulk2"], 3, 900))

# ------------------------------------------------------------------ username claim
with SessionLocal() as db:
    db.add(mp.ProvisioningOperation(operation_type="create_user", business_key="req:x:claim", request_hash="0" * 64,
        tenant_scope_key="shared", actor_kind="system", intent="{}", state="provisioning", username_claim="held",
        forward_deadline=dt.datetime.utcnow(), wallet_epoch_at_start=0))
    db.commit()
for kind in ("create_user",):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "legacy"
        db.commit()
with SessionLocal() as db:
    root_admin = db.query(models.AdminUser).filter_by(username="root").one()
    try:
        users_router.create_user(schemas.UserCreate(username="held"), db=db, admin=root_admin, _perm=None,
                                 x_idempotency_key=None)
        claimed = "created"
    except HTTPException as exc:
        claimed = exc.status_code
check("a username held by an unfinished durable create is refused on the legacy path", claimed, 400)

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
