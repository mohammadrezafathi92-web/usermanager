"""Receipt approvals on the durable path: replay, the access boundary of the
bot's separate calls, and frozen referral effects (Lifecycle 7.6, Receipt
Void 6.5, 5.3). Fake runner; no network.

  * referral rewards are applied as FROZEN at registration: the panel's
    reward settings changed between T1 and T_final change nothing; effects
    equal the manifest;
  * the same production call again with the same token: the same operation,
    no second customer, service or charge;
  * apply_referral / redeem_discount / record-payment with the approval:
    no token -> 422; an old token after a takeover -> 409; another execution
    key -> 403; a scoped bot of another tenant -> 403; another card or
    customer than the approval's -> 409 - nothing disclosed or applied;
  * an existing customer buying with a referral code and zero rewards (no
    referral row in the manifest): the referral link is NOT written.
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-approval-boundary-")
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
HOST = HostIdentity("d4" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1, referral_referrer_reward_credit=50, referral_new_user_reward_credit=20))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    other = models.AdminUser(username="other", hashed_password="x", balance=0)
    key_b = models.ApiKey(key="remote-b", label="b", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    db.add_all([root, other, key_b])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.20.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.92.0.0/24")
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
    card_a = models.PaymentCard(owner_admin_id=None, card_number="6037000000000001", is_active=True)
    card_b = models.PaymentCard(owner_admin_id=None, card_number="6037000000000002", is_active=True)
    db.add_all([card_a, card_b])
    referrer = wallet_accounts.create_user_with_wallet(db, username="referrer", telegram_id=1101, balance=0)
    referrer.referral_code = "REF1"
    wallet_accounts.create_user_with_wallet(db, username="oldie", telegram_id=1102)
    db.commit()
    PACKAGE, NODE, CARD_A, CARD_B, OTHER, KEY_B = package.id, node.id, card_a.id, card_b.id, other.id, key_b.id
    db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="shadow", key_identity_ready=True,
                                                                 key_identity_failure=None))
    for kind in ("create_user", "purchase"):
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
    db.commit()
    REMOTE_B = bot_auth.BotPrincipal.from_api_key(db.get(models.ApiKey, KEY_B))
INTERNAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
SCOPED_OTHER = bot_auth.BotPrincipal.internal(OTHER, scope_enforced=True)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def settings_change_mid_flight(dto):
    with SessionLocal() as db:                    # between T1 and T_final
        row = db.get(models.PanelSettings, 1)
        row.referral_referrer_reward_credit, row.referral_new_user_reward_credit = 999, 999
        db.commit()
    return ok_runner(dto)


def register(intent):
    with SessionLocal() as db:
        result = registration.register(db, INTERNAL, intent, approval_mode="auto")
        db.commit()
        return result.approval_uuid, result.execution_token


def code(call):
    try:
        return call()
    except HTTPException as exc:
        return exc.status_code, exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")


# ------------------------------------------------------------------ frozen referral
uuid_a, token_a = register(ri.ApprovalIntent(pending_source_instance_id="b", pending_local_id=1, kind="new",
                                             target_username="newbie", amount=1000, package_id=PACKAGE,
                                             payment_card_id=CARD_A, referral_code="REF1", list_price=1000,
                                             claimed_telegram_id=1201))


def create(runner, token=None):
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        return bot_router.create_user(schemas.BotCreateUserRequest(
            username="newbie", telegram_id=1201, package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(
                node_id=NODE, protocol="wireguard")], paid_amount=1000, payment_method="card", payment_card_id=CARD_A,
            approval_uuid=uuid_a, execution_token=token_a if token is None else token, referral_code="REF1",
            list_price=1000), db=db, principal=INTERNAL)


create(settings_change_mid_flight)
with SessionLocal() as db:
    approval = db.execute(select(rv.receipt_approvals).where(rv.receipt_approvals.c.approval_uuid == uuid_a)).mappings().one()
    written = sorted(r.effect_key for r in db.execute(select(rv.receipt_approval_effects.c.effect_key).where(
        rv.receipt_approval_effects.c.approval_uuid == uuid_a)))
    expected = sorted(r.effect_key for r in db.execute(select(rv.receipt_approval_expected_effects.c.effect_key).where(
        rv.receipt_approval_expected_effects.c.approval_uuid == uuid_a)))
    newbie = db.query(models.User).filter_by(username="newbie").one()
    check("settings changed between T1 and T_final: the FROZEN rewards (50 / 20) are applied",
          (db.query(models.User).filter_by(username="referrer").one().balance, newbie.balance), (50, 20))
    check("...the effects equal the manifest and the approval completed",
          (written == expected, "credit:referral_reward:referrer" in written, approval["state"]), (True, True, "completed"))
    counts = (db.query(models.User).count(), db.query(models.Purchase).count(), db.query(models.LedgerEntry).count())

# ------------------------------------------------------------------ approval retry
create(ok_runner)
with SessionLocal() as db:
    check("the same approval call again with the same token: the same operation, nothing new",
          (db.query(models.User).count(), db.query(models.Purchase).count(), db.query(models.LedgerEntry).count(),
           db.query(mp.ProvisioningOperation).filter_by(approval_uuid=uuid_a).count()), counts + (1,))

# ------------------------------------------------------------------ the separate calls' boundary
def referral(principal, token, username="newbie"):
    with SessionLocal() as db:
        return code(lambda: bot_router.apply_referral(schemas.ReferralApplyRequest(
            username=username, referral_code="REF1", approval_uuid=uuid_a, execution_token=token),
            db=db, principal=principal))


def card(principal, token, card_id):
    with SessionLocal() as db:
        return code(lambda: bot_router.record_payment_card_use(card_id, schemas.BotRecordCardPaymentRequest(
            amount=1000, approval_uuid=uuid_a, execution_token=token), db=db, principal=principal))


def discount(principal, token):
    with SessionLocal() as db:
        return code(lambda: bot_router.redeem_discount(schemas.DiscountRedeemRequest(
            code="X", username="newbie", package_price=1000, approval_uuid=uuid_a, execution_token=token),
            db=db, principal=principal))


check("referral with the current token: answered from the operation", referral(INTERNAL, token_a),
      {"ok": True, "reason": ""})
check("no token: refused", referral(INTERNAL, None), (422, "approval_execution_token_required"))
check("another execution key (remote bot B): refused for referral / discount / card",
      (referral(REMOTE_B, token_a)[0], discount(REMOTE_B, token_a)[0], card(REMOTE_B, token_a, CARD_A)[0]),
      (403, 403, 403))
check("a scoped bot of another tenant: refused", referral(SCOPED_OTHER, token_a)[0], 403)
check("another customer than the approval's: refused", referral(INTERNAL, token_a, username="oldie"),
      (409, "approval_resource_mismatch"))
check("another card than the approval's: refused", card(INTERNAL, token_a, CARD_B), (409, "approval_resource_mismatch"))
check("the approval's own card: answered, counted once", card(INTERNAL, token_a, CARD_A), {"ok": True})
with SessionLocal() as db:
    db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == uuid_a)
               .values(version=rv.receipt_approvals.c.version + 1))       # a takeover moved the token on
    db.commit()
check("an old token after the takeover: refused (referral / card)",
      (referral(INTERNAL, token_a)[0], card(INTERNAL, token_a, CARD_A)[0]), (409, 409))
with SessionLocal() as db:
    check("...nothing was applied twice: balances unchanged, one card payment",
          (db.query(models.User).filter_by(username="referrer").one().balance,
           db.get(models.PaymentCard, CARD_A).accumulated_amount), (50, 1000))

# ------------------------------------------------------------------ existing customer, zero rewards
with SessionLocal() as db:
    row = db.get(models.PanelSettings, 1)
    row.referral_referrer_reward_credit = row.referral_new_user_reward_credit = 0
    db.commit()
uuid_b, token_b = register(ri.ApprovalIntent(pending_source_instance_id="b", pending_local_id=2, kind="new",
                                             target_username="oldie", amount=1000, package_id=PACKAGE,
                                             referral_code="REF1", list_price=1000, claimed_telegram_id=1102))
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    bot_router.purchase_package("oldie", schemas.BotPurchasePackageRequest(
        package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
        paid_amount=1000, payment_method="card", approval_uuid=uuid_b, execution_token=token_b,
        referral_code="REF1", list_price=1000), db=db, owner_admin_id=None, principal=INTERNAL)
with SessionLocal() as db:
    oldie = db.query(models.User).filter_by(username="oldie").one()
    state = db.execute(select(rv.receipt_approvals.c.state).where(rv.receipt_approvals.c.approval_uuid == uuid_b)).scalar()
    check("existing customer, referral code, zero rewards: no referral row, so the link is NOT written",
          (oldie.referred_by_id, oldie.referral_reward_granted, state), (None, False, "completed"))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
