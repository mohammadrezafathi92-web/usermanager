"""(harness copied from test_durable_retry_replay) Retries reach the SAME durable operation (Lifecycle 13). Fake runner; no
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

DIRECTORY = tempfile.mkdtemp(prefix="um-panel-onetime-")
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
        adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
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



# ------------------------------------------------------------------ panel trial
# Production (2026-11-12): the panel's "create user" with a one-time
# (free-trial) package and no Telegram id failed with
# one_time_claim_identity_invalid. One-time limits customers in a bot; the
# panel may always grant it (owner rule 2026-09-09).
def panel_create(name, admin_id, **extra):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        admin = db.get(models.AdminUser, admin_id)
        try:
            user = users_router.create_user(schemas.UserCreate(username=name, package_id=ONCE, **extra), db=db,
                                            admin=admin, _perm=None, x_idempotency_key=None)
            return ("created", len(user.purchases))
        except HTTPException as exc:
            return exc.detail


check("panel: a one-time package for a new user without a Telegram id is created",
      panel_create("trial-a", RESELLER), ("created", 1))
check("panel: the same package again for another new user is created too", panel_create("trial-b", RESELLER),
      ("created", 1))
first_bot = buy(ONCE, str(uuid.uuid4()))
check("bot: a customer's first one-time purchase is fine", isinstance(first_bot, list), True)
check("bot: the one-time rule still refuses the second", isinstance(buy(ONCE, str(uuid.uuid4())), str), True)

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall panel one-time checks passed")
