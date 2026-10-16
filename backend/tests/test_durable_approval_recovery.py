"""Receipt approvals on the durable path when things go wrong (Lifecycle
7.6, 7.7, 11.4, 11.5). Fake runner; no network.

  R1 a remote failure that cannot roll forward: compensated; T_comp fails
     the approval (no effect), the reseller's credit released, nothing left;
  R2 'required' T_final whose effect cannot be recorded: the whole T_final
     rolled back, compensation (final_tx_failed_permanent), approval failed;
  R3 a crash between the remote write and T_final's commit: the approval
     stays mutating with no effect; the worker completes it later;
  R4 the execution key revoked after T1: no T_final - cleanup_required plus
     ONE approval_recovery, money still reserved; the bot's failure report
     does not fail it; superadmin 'continue' -> T_final with the recovery in
     place of the key check -> completed, the recovery closed with its actor;
  R5 the same, 'cancel' -> compensation and T_comp -> approval failed;
  R6 'shadow' T_final whose effect fails: the sale completes, the approval
     stays mutating with a shadow event and an approval_recovery in the same
     commit; 'continue' accepts it; 'cancel' is refused (a void is needed);
  R7 the dispatch binding refuses a forward write for an approval that is
     not mutating at the operation's version, or whose key is disabled.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-approval-recovery-")
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
from app.services import bot_auth, provisioning_approval_final as approval_final
from app.services import provisioning_contracts as contracts, provisioning_dispatch_binding as dispatch
from app.services import provisioning_durable as durable, provisioning_operation_worker as worker
from app.services import provisioning_parent_execute as execute, provisioning_schema, receipt_void_schema
from app.services import receipt_approval_effects as effects, receipt_approval_intent as ri
from app.services import receipt_approval_registration as registration, remote_action as action
from app.services import user_ops, wallet_accounts
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
HOST = HostIdentity("e8" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=10_000)
    key = models.ApiKey(key="remote-key", label="remote", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    db.add_all([root, reseller, key])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.15.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.51.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version(), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
    shared = models.Package(name="Shared", quota_gb=10, duration_days=30, price=1000)
    owned = models.Package(name="Owned", quota_gb=5, duration_days=30, price=500, cooperation_price=300,
                           owner_admin_id=reseller.id)
    db.add_all([shared, owned])
    db.flush()
    for package in (shared, owned):
        db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
    wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=802, owner_admin_id=reseller.id)
    for index in range(1, 5):
        wallet_accounts.create_user_with_wallet(db, username=f"plain{index}", telegram_id=810 + index)
    db.commit()
    SHARED, OWNED, NODE, RESELLER, KEY, ROOT = shared.id, owned.id, node.id, reseller.id, key.id, root.id
    db.execute(rv.receipt_approval_runtime_state.update().values(
        registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1,
        key_identity_ready=True))
    db.commit()
    REMOTE = bot_auth.BotPrincipal.from_api_key(db.get(models.ApiKey, KEY))
for kind in ("create_user", "purchase", "renew_purchase"):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
INTERNAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))
SEQ = iter(range(1, 1000))


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def register(username, package, amount, principal=INTERNAL, mode="required"):
    item = ri.ApprovalIntent(pending_source_instance_id="bot-recovery", pending_local_id=next(SEQ), kind="new",
                             target_username=username, amount=amount, package_id=package, list_price=amount)
    with SessionLocal() as db:
        result = registration.register(db, principal, item, approval_mode="auto")
        if mode == "required":
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == result.approval_uuid).values(
                    registered_under_mode="required", completeness_generation=1))
        db.commit()
        return result.approval_uuid, result.execution_token


def buy(username, package, amount, approval_uuid, token, principal=INTERNAL, runner=ok_runner):
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        try:
            bot_router.purchase_package(username, schemas.BotPurchasePackageRequest(
                package_id=package, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
                paid_amount=amount, payment_method="card", approval_uuid=approval_uuid, execution_token=token,
                list_price=amount), db=db, owner_admin_id=None, principal=principal)
            return "ok"
        except HTTPException as exc:
            return exc.detail if isinstance(exc.detail, str) else (exc.status_code, exc.detail.get("error"),
                                                                   exc.detail.get("state"))


def state(approval_uuid):
    with SessionLocal() as db:
        row = db.execute(select(rv.receipt_approvals).where(
            rv.receipt_approvals.c.approval_uuid == approval_uuid)).mappings().one()
        count = len(db.execute(select(rv.receipt_approval_effects.c.id).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid)).all())
        operation = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.approval_uuid == approval_uuid)).scalar_one_or_none()
        reservations = [r.state for r in db.query(mp.PaymentReservation).filter_by(
            operation_id=operation.id)] if operation else []
        recovery = db.execute(select(rv.wallet_operations.c.state, rv.wallet_operations.c.actor_id).where(
            rv.wallet_operations.c.operation_type == "approval_recovery",
            rv.wallet_operations.c.business_key == approval_uuid)).first()
        return dict(approval=row["state"], effects=count, operation=operation.state if operation else None,
                    error=operation.error_code if operation else None, reservations=reservations,
                    recovery=tuple(recovery) if recovery else None)


def purchases(username):
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(username=username).one()
        return len(user.purchases), len(user.connections)


def recover():
    with NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
        for _ in range(6):
            durable.recover_due(SessionLocal)


# ------------------------------------------------------------------ R1 remote failure
def failing(dto):
    if dto.action_type is not action.ActionType.WG_ENSURE_PRESENT:
        return ok_runner(dto)
    with SessionLocal() as db:
        db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.state.notin_(
            ("completed", "compensated"))).update({"forward_deadline": dt.datetime(2000, 1, 1)})
        db.commit()
    return action.RemoteActionResult(dto.action_id, action.Outcome.TRANSPORT_ERROR, write_attempted=False,
                                     error_code="connect_failed")


with SessionLocal() as db:
    before = db.get(models.AdminUser, RESELLER).balance
u1, t1 = register("cust", OWNED, 500)
r1 = buy("cust", OWNED, 500, u1, t1, runner=failing)
with SessionLocal() as db:
    after = db.get(models.AdminUser, RESELLER).balance
check("R1 remote failure: 400, compensated; the approval failed with no effect; credit released; nothing left",
      (r1[0] if isinstance(r1, tuple) else r1, state(u1)["operation"], state(u1)["approval"], state(u1)["effects"],
       state(u1)["reservations"], after - before, purchases("cust")),
      (400, "compensated", "failed", 0, ["released"], 0, (0, 0)))

# ------------------------------------------------------------------ R2 required effect failure
real_record = effects.record_effect


def no_connection_effect(db, approval_uuid, effect_type, *args, **kwargs):
    if effect_type == "connection_created":
        raise effects.EffectRejected(effects.MANIFEST_MISMATCH, "injected")
    return real_record(db, approval_uuid, effect_type, *args, **kwargs)


u2, t2 = register("cust", OWNED, 500)
with patch.object(effects, "record_effect", side_effect=no_connection_effect):
    r2 = buy("cust", OWNED, 500, u2, t2)
check("R2 required T_final with a failing effect: rolled back whole, compensated, approval failed",
      (r2[0] if isinstance(r2, tuple) else r2, state(u2)["operation"], state(u2)["error"], state(u2)["approval"],
       state(u2)["effects"], state(u2)["reservations"], purchases("cust")),
      (400, "compensated", "final_tx_failed_permanent", "failed", 0, ["released"], (0, 0)))

# ------------------------------------------------------------------ R3 crash between remote and commit
u3, t3 = register("plain1", SHARED, 1000)
real_finish = worker.final.finish
crashes = {"n": 0}


def crash_once(*args, **kwargs):
    if crashes["n"] == 0:
        crashes["n"] += 1
        raise RuntimeError("process killed before T_final committed")
    return real_finish(*args, **kwargs)


with patch.object(worker.final, "finish", side_effect=crash_once):
    try:
        buy("plain1", SHARED, 1000, u3, t3)
        crashed = "no crash"
    except RuntimeError:
        crashed = "crashed"
middle = state(u3)
recover()
check("R3 crash before T_final's commit: nothing of T_final committed (the step stays recorded), approval "
      "mutating, no effect", (crashed, middle["operation"], middle["approval"], middle["effects"]),
      ("crashed", "provisioning", "mutating", 0))
check("R3 ...the worker completes it later: approval completed with its effects, one service",
      (state(u3)["operation"], state(u3)["approval"], state(u3)["effects"], purchases("plain1")),
      ("completed", "completed", 4, (1, 1)))          # + the counted purchase ('payment_event', P9a)

# ------------------------------------------------------------------ R4 key revoked after T1 -> continue
def revoke_then_succeed(dto):
    with SessionLocal() as db:
        db.get(models.ApiKey, KEY).enabled = False
        db.commit()
    return ok_runner(dto)


u4, t4 = register("plain2", SHARED, 1000, principal=REMOTE)
r4 = buy("plain2", SHARED, 1000, u4, t4, principal=REMOTE, runner=revoke_then_succeed)
s4 = state(u4)
check("R4 key revoked mid-execution: 409 in progress, cleanup_required + one approval_recovery, no T_final",
      (r4, s4["operation"], s4["error"], s4["approval"], s4["effects"], s4["recovery"]),
      ((409, "operation_in_progress", "cleanup_required"), "cleanup_required", "execution_key_revoked",
       "mutating", 0, ("cleanup_required", None)))
with SessionLocal() as db:
    try:
        registration.finalize(db, REMOTE, u4, reported_failure=True)
        db.commit()
        finalized = "finalized"
    except registration.RegistrationRejected as exc:
        db.rollback()
        finalized = exc.code
with SessionLocal() as db:
    db.get(models.ApiKey, KEY).enabled = True               # the same key back: finalize only reads
    db.commit()
with SessionLocal() as db:
    reread = registration.finalize(db, REMOTE, u4, reported_failure=True)
    db.commit()
with SessionLocal() as db:
    db.get(models.ApiKey, KEY).enabled = False
    db.commit()
check("R4 the bot's failure report does not fail it: a revoked key is refused, the key's report only reads",
      (finalized, reread["state"], reread["operation_state"], state(u4)["approval"]),
      ("key_identity_missing", "mutating", "cleanup_required", "mutating"))
recover()
check("R4 the worker does not finish it on its own", state(u4)["operation"], "cleanup_required")
decision = approval_final.decide(SessionLocal, u4, "continue", ROOT, "key rotated by the operator")
recover()
s4 = state(u4)
check("R4 superadmin continue: T_final with the recovery in place of the key -> completed, recovery closed",
      (decision["decision"], s4["operation"], s4["approval"], s4["effects"], s4["recovery"], purchases("plain2")),
      ("continue", "completed", "completed", 4, ("completed", ROOT), (1, 1)))

# ------------------------------------------------------------------ R5 key revoked -> cancel
with SessionLocal() as db:
    db.get(models.ApiKey, KEY).enabled = True
    db.commit()
u5, t5 = register("plain3", SHARED, 1000, principal=REMOTE)
buy("plain3", SHARED, 1000, u5, t5, principal=REMOTE, runner=revoke_then_succeed)
approval_final.decide(SessionLocal, u5, "cancel", ROOT, "receipt was fake")
recover()
s5 = state(u5)
check("R5 superadmin cancel: compensation removes the remote, T_comp fails the approval, recovery closed",
      (s5["operation"], s5["error"], s5["approval"], s5["effects"], s5["recovery"], purchases("plain3")),
      ("compensated", "cancelled_by_recovery", "failed", 0, ("completed", ROOT), (0, 0)))

# ------------------------------------------------------------------ R6 shadow effect failure
with SessionLocal() as db:
    db.get(models.ApiKey, KEY).enabled = True
    db.execute(rv.receipt_approval_runtime_state.update().values(
        registration_mode="shadow", loyalty_timing_mode="legacy_activation"))
    db.commit()
u6, t6 = register("plain4", SHARED, 1000, mode="shadow")
with patch.object(effects, "record_effect", side_effect=no_connection_effect):
    r6 = buy("plain4", SHARED, 1000, u6, t6)
s6 = state(u6)
with SessionLocal() as db:
    events = [r.error_code for r in db.execute(select(rv.receipt_approval_shadow_events.c.error_code).where(
        rv.receipt_approval_shadow_events.c.approval_uuid == u6))]
check("R6 shadow: the sale completes, the approval stays mutating, shadow events and one approval_recovery",
      (r6, s6["operation"], s6["approval"], s6["effects"], s6["recovery"], "missing_effects" in events,
       purchases("plain4")), ("ok", "completed", "mutating", 2, ("cleanup_required", None), True, (1, 1)))
try:
    approval_final.decide(SessionLocal, u6, "cancel", ROOT, "no")
    refused = "cancelled"
except HTTPException as exc:
    refused = exc.detail
approval_final.decide(SessionLocal, u6, "continue", ROOT, "checked by hand")
check("R6 cancel of a completed sale is refused (a void is needed); continue accepts what was recorded",
      (refused, state(u6)["approval"], state(u6)["recovery"]),
      ("approval_recovery_cancel_requires_void", "completed", ("completed", ROOT)))

# ------------------------------------------------------------------ R7 dispatch binding
live = dict(approval_state="mutating", approval_version=2, approval_execution_version=2, approval_key_instance="k",
            op_key_instance="k", approval_key_bound=True, recovery_running=0, key_enabled=True,
            key_type="remote_shared_bot")
check("R7 dispatch binding: a mutating approval with its live key may write forward", dispatch._approval_live(live))
check("R7 ...not after a takeover, not when not mutating, not with a disabled key (unless recovery runs)",
      (dispatch._approval_live({**live, "approval_version": 3}), dispatch._approval_live({**live, "approval_state": "failed"}),
       dispatch._approval_live({**live, "key_enabled": False}),
       dispatch._approval_live({**live, "key_enabled": False, "recovery_running": 1})), (False, False, False, True))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
