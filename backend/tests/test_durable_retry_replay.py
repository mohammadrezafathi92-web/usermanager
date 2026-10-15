"""Retries reach the SAME durable operation (Lifecycle 13). Fake runner; no
network.

  * bot purchase_package, same X-Idempotency-Key twice: one operation, one
    Purchase, one charge, no second remote write - also for a one-time
    package, which a retry must not refuse as "already bought";
  * the Mini App wallet checkout: the answer lost after the commit, the page
    retries with the same key: same operation, one debit, one service;
  * panel bulk create, the same token twice, the first run left one member
    unfinished: the retry finishes exactly that member - no new index, no
    extra charge;
  * deletions (bot purchase / panel user), the same key again after success:
    answered from the operation, not 404;
  * no new legacy account: creating an account without a package is refused
    before any write (panel, superadmin included; bot).
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-retry-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException

from app import models, models_provisioning as mp, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, miniapp, users as users_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import remote_action as action, user_ops, wallet_accounts
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("c3" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=10_000)
    db.add_all([root, reseller])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.19.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.91.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
    package = models.Package(name="P", quota_gb=5, duration_days=30, price=400, cooperation_price=300,
                             owner_admin_id=reseller.id)
    once = models.Package(name="Once", quota_gb=1, duration_days=7, price=100, cooperation_price=50,
                          owner_admin_id=reseller.id, one_time_per_user=True)
    db.add_all([package, once])
    db.flush()
    for item in (package, once):
        db.add(models.PackageConnection(package_id=item.id, node_id=node.id, protocol="wireguard"))
    wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=990, owner_admin_id=reseller.id,
                                            balance=5000)
    db.commit()
    ROOT, RESELLER, PACKAGE, ONCE = root.id, reseller.id, package.id, once.id
for kind in mp.OPERATION_TYPES:
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))
calls = []


def ok_runner(dto):
    calls.append(dto.action_type)
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


RUNNER = patch.object(execute.remote_runner, "run_action", side_effect=ok_runner)


def state():
    with SessionLocal() as db:
        return dict(ops=db.query(mp.ProvisioningOperation).count(), purchases=db.query(models.Purchase).count(),
                    reseller=db.get(models.AdminUser, RESELLER).balance,
                    wallet=db.query(models.User).filter_by(username="cust").one().balance)


def buy(package_id, key, **extra):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        try:
            out = bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
                package_id=package_id, paid_amount=400 if package_id == PACKAGE else 100, payment_method="card",
                **extra), db=db, owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=key)
            return [c.id for c in out.connections]
        except HTTPException as exc:
            return exc.detail


# ------------------------------------------------------------------ purchase retry
key = str(uuid.uuid4())
first = buy(PACKAGE, key)
before, sent = state(), len(calls)
again = buy(PACKAGE, key)
check("purchase, same key again: the same connections answered", again, first)
check("...no second operation, Purchase or charge, and no second remote write",
      (state(), len(calls)), (before, sent))
key = str(uuid.uuid4())
first_once = buy(ONCE, key)
before = state()
check("a one-time package retried with the same key: answered, not refused as already bought",
      (buy(ONCE, key), state()), (first_once, before))
check("...a NEW request for it is still refused", isinstance(buy(ONCE, str(uuid.uuid4())), str), True)

# ------------------------------------------------------------------ Mini App: answer lost after the commit
with SessionLocal() as db:
    db.query(models.User).filter_by(username="cust").update({"balance": 1000})
    db.commit()


def checkout(key):
    with SessionLocal() as db, NO_SHARE, RUNNER, \
            patch.object(miniapp, "_package_or_404", side_effect=lambda d, o, i: d.get(models.Package, i)), \
            patch.object(miniapp, "_notify_owner_of_sale", return_value=None):
        return miniapp.checkout(miniapp.CheckoutRequest(package_id=PACKAGE), visitor={
            "owner_admin_id": None, "telegram_id": 990, "user": {}}, db=db, x_idempotency_key=key)


page_key = str(uuid.uuid4())
checkout(page_key)                         # committed; the page never saw the answer
before, sent = state(), len(calls)
retried = checkout(page_key)
check("Mini App retry with the page's key after a lost answer: done, the same operation",
      (retried["status"], state(), len(calls)), ("done", before, sent))
check("...the wallet was debited exactly once (1000 - 400)", before["wallet"], 600)

# ------------------------------------------------------------------ bulk create: the same token, one member unfinished
token = str(uuid.uuid4())
unfinished = {"done": False, "present": 0}


def second_member_hangs(dto):
    calls.append(dto.action_type)
    if dto.action_type is action.ActionType.WG_ENSURE_PRESENT:
        unfinished["present"] += 1
    if not unfinished["done"] and unfinished["present"] == 2:
        unfinished["done"] = True
        return action.RemoteActionResult(dto.action_id, action.Outcome.TIMEOUT_UNKNOWN, write_attempted=True,
                                         error_code="timeout")
    return ok_runner(dto)


def bulk(runner):
    with SessionLocal() as db, patch.object(execute.remote_runner, "run_action", side_effect=runner), \
            patch.object(durable, "REQUEST_BUDGET_SECONDS", 2):
        admin = db.get(models.AdminUser, RESELLER)
        return users_router.bulk_create_users(schemas.BulkCreateUsersRequest(prefix="bulk", count=3, package_id=PACKAGE),
                                              db=db, admin=admin, _perm=None, x_idempotency_key=token)


first_run = bulk(second_member_hangs)
with SessionLocal() as db:
    keys_after_first = sorted(op.business_key for op in db.query(mp.ProvisioningOperation).filter(
        mp.ProvisioningOperation.business_key.like(f"bulk:{token}:%")))
    balance_after_first = db.get(models.AdminUser, RESELLER).balance     # all three reserved in their T1
with SessionLocal() as db:                 # the unfinished member's step becomes due again
    db.query(mp.ProvisioningStep).update({"next_retry_at": None})
    db.query(mp.ProvisioningOperation).update({"next_retry_at": None})
    db.commit()
second_run = bulk(ok_runner)
with SessionLocal() as db:
    keys_after_second = sorted(op.business_key for op in db.query(mp.ProvisioningOperation).filter(
        mp.ProvisioningOperation.business_key.like(f"bulk:{token}:%")))
    states = sorted(op.state for op in db.query(mp.ProvisioningOperation).filter(
        mp.ProvisioningOperation.business_key.like(f"bulk:{token}:%")))
check("first run: one member left unfinished, reported in progress and NOT replaced by another index",
      (unfinished["done"], first_run["in_progress"], sorted(first_run["created"]), len(keys_after_first)),
      (True, ["bulk2"], ["bulk1", "bulk3"], 3))
check("the retry with the same token: exactly the same indexes, no new member",
      (keys_after_second, len(keys_after_second)), (keys_after_first, 3))
check("...every member complete, the same three names, each charged once",
      (states, sorted(second_run["created"]), state()["reseller"] == balance_after_first),
      (["completed"] * 3, ["bulk1", "bulk2", "bulk3"], True))

# ------------------------------------------------------------------ deletions answered from the operation
with SessionLocal() as db:
    purchase_id = db.query(models.Purchase).join(models.User).filter(models.User.username == "cust").first().id
    db.query(models.Purchase).filter_by(id=purchase_id).update({"status": models.UserStatus.expired})
    db.commit()
delete_key = str(uuid.uuid4())


def delete_purchase():
    with SessionLocal() as db, RUNNER:
        try:
            return bot_router.delete_purchase("cust", purchase_id, db=db, owner_admin_id=None, principal=PRINCIPAL,
                                              x_idempotency_key=delete_key)["ok"]
        except HTTPException as exc:
            return exc.status_code


check("bot delete_purchase", delete_purchase(), True)
check("...the same key again after success: answered from the operation, not 404", delete_purchase(), True)
with SessionLocal() as db:
    victim = db.query(models.User).filter_by(username="bulk3").one().id
panel_key = str(uuid.uuid4())


def delete_user():
    with SessionLocal() as db, RUNNER:
        admin = db.get(models.AdminUser, RESELLER)
        try:
            return users_router.delete_user(victim, db=db, admin=admin, _confirm=None, _perm=None,
                                            x_idempotency_key=panel_key)["ok"]
        except HTTPException as exc:
            return exc.status_code


check("panel delete_user", delete_user(), True)
check("...the same key again: answered, not 404", delete_user(), True)

# ------------------------------------------------------------------ no new legacy account
with SessionLocal() as db:
    root_admin = db.get(models.AdminUser, ROOT)
    try:
        users_router.create_user(schemas.UserCreate(username="nopkg"), db=db, admin=root_admin, _perm=None,
                                 x_idempotency_key=None)
        panel = "created"
    except HTTPException as exc:
        panel = exc.status_code
    try:
        bot_router.create_user(schemas.BotCreateUserRequest(username="nopkg2", telegram_id=991), db=db,
                               principal=PRINCIPAL)
        bot = "created"
    except HTTPException as exc:
        bot = exc.detail
    check("no package: refused before any write (panel superadmin, bot)",
          (panel, bot, db.query(models.User).filter(models.User.username.in_(("nopkg", "nopkg2"))).count()),
          (400, "package_required", 0))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
