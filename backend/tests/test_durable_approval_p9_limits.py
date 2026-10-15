"""What the durable approval path does NOT do before Receipt Void P9/P9a,
made explicit and fail-closed (not hidden by readiness). Fake runner.

  * loyalty: a 'required' approval writes NO legacy loyalty reward (it would
    be an effect no manifest describes; P9a's payment-event loyalty owns
    it); a 'shadow' approval keeps today's legacy loyalty;
  * the wallet after its cutover ('enforced'): an approval's T1 is refused
    (503) - the reservations of this phase are legacy-balance ones only.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-approval-p9-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException
from sqlalchemy import select

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
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
HOST = HostIdentity("e5" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1, loyalty_purchase_threshold=1, loyalty_reward_credit=77))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    db.add(root)
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.21.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.93.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    package = models.Package(name="Shared", quota_gb=10, duration_days=30, price=1000)
    db.add(package)
    db.flush()
    db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
    for name, tg in (("strict", 1301), ("loose", 1302), ("later", 1303)):
        wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg)
    db.commit()
    PACKAGE, NODE = package.id, node.id
    db.execute(rv.receipt_approval_runtime_state.update().values(
        registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1,
        key_identity_ready=True, key_identity_failure=None))
    db.get(mp.ProvisioningTypeMode, "purchase").mode = "durable"
    db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))
SEQ = iter(range(1, 100))


def ok_runner(dto):
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def approve(username, mode):
    item = ri.ApprovalIntent(pending_source_instance_id="p9", pending_local_id=next(SEQ), kind="new",
                             target_username=username, amount=1000, package_id=PACKAGE, list_price=1000)
    with SessionLocal() as db:
        result = registration.register(db, PRINCIPAL, item, approval_mode="auto")
        if mode == "required":
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == result.approval_uuid).values(
                    registered_under_mode="required", completeness_generation=1))
        db.commit()
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
        try:
            bot_router.purchase_package(username, schemas.BotPurchasePackageRequest(
                package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
                paid_amount=1000, payment_method="card", approval_uuid=result.approval_uuid,
                execution_token=result.execution_token, list_price=1000), db=db, owner_admin_id=None, principal=PRINCIPAL)
            outcome = "ok"
        except HTTPException as exc:
            outcome = (exc.status_code, exc.detail)
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(username=username).one()
        state = db.execute(select(rv.receipt_approvals.c.state).where(
            rv.receipt_approvals.c.approval_uuid == result.approval_uuid)).scalar()
        return outcome, state, user.balance, user.loyalty_rewards_given or 0


check("'required' approval with loyalty due: completed, NO legacy loyalty reward (P9a owns it)",
      approve("strict", "required"), ("ok", "completed", 0, 0))
with SessionLocal() as db:
    check("...the purchase itself is still counted (a durable purchase keeps purchase_count)",
          db.query(models.User).filter_by(username="strict").one().purchase_count, 1)
    db.execute(rv.receipt_approval_runtime_state.update().values(
        registration_mode="shadow", loyalty_timing_mode="legacy_activation"))
    db.commit()
check("'shadow' approval: today's legacy loyalty is kept", approve("loose", "shadow"), ("ok", "completed", 77, 1))
with SessionLocal() as db:
    db.execute(rv.wallet_runtime_state.update().values(phase="enforced"))
    db.commit()
outcome = approve("later", "shadow")
check("the wallet after its cutover ('enforced'): the approval's T1 is refused, nothing bought",
      (outcome[0][0] if isinstance(outcome[0], tuple) else outcome[0], outcome[1]), (503, "registered"))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
