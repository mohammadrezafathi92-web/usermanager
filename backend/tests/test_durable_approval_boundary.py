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
    referral row in the manifest): the referral link is NOT written;
  * a volume-only referrer reward: the referrer is frozen by its quota
    binding; a code that changed owner between T1 and T_final links nobody
    else (the new owner is never linked while the frozen reward is due to
    the first), and an unchanged code links and rewards the frozen one;
  * a referral that rewards only the new customer: the manifest freezes
    the referrer's identity on its own (a zero, optional credit row), the
    approval executes and links exactly that referrer (receipt #1099); a
    manifest registered before that row existed is still refused
    fail-closed before any write.
"""
import datetime as dt
import json
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

# ------------------------------------------------------------------ volume-only referral: the referrer is frozen too
GB = 1024 ** 3
with SessionLocal() as db:
    row = db.get(models.PanelSettings, 1)
    row.referral_referrer_reward_gb, row.referral_new_user_reward_gb = 1, 0      # credits are 0 since above
    for name, tg, quota in (("volref", 1401, 10 * GB), ("thief", 1402, 10 * GB), ("volref2", 1403, 10 * GB)):
        user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg)
        user.total_quota_bytes = quota
        user.referral_code = {"volref": "VOL1", "volref2": "VOL2"}.get(name)
    db.commit()


def quotas(*names):
    with SessionLocal() as db:
        return tuple(int(db.query(models.User).filter_by(username=n).one().total_quota_bytes) for n in names)


def volume_signup(local_id, username, tg, referral_code, runner):
    approval_uuid, token = register(ri.ApprovalIntent(
        pending_source_instance_id="b", pending_local_id=local_id, kind="new", target_username=username,
        amount=1000, package_id=PACKAGE, referral_code=referral_code, list_price=1000, claimed_telegram_id=tg))
    with SessionLocal() as db:
        frozen = [r.effect_key for r in db.execute(select(rv.receipt_approval_expected_effects.c.effect_key).where(
            rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid))]
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        code(lambda: bot_router.create_user(schemas.BotCreateUserRequest(
            username=username, telegram_id=tg, package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(
                node_id=NODE, protocol="wireguard")], paid_amount=1000, payment_method="card",
            approval_uuid=approval_uuid, execution_token=token, referral_code=referral_code, list_price=1000),
            db=db, principal=INTERNAL))
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(username=username).one_or_none()
        return frozen, (user.referred_by_id if user else "no user")


def code_moves_mid_flight(dto):
    with SessionLocal() as db:                    # between T1 and T_final the code changes owner
        db.query(models.User).filter_by(username="volref").update({"referral_code": None})
        db.query(models.User).filter_by(username="thief").update({"referral_code": "VOL1"})
        db.commit()
    return ok_runner(dto)


with SessionLocal() as db:
    VOLREF, THIEF, VOLREF2 = (db.query(models.User).filter_by(username=n).one().id for n in ("volref", "thief", "volref2"))
before = quotas("volref", "thief")
frozen, linked = volume_signup(3, "volnew", 1501, "VOL1", code_moves_mid_flight)
check("volume-only reward: the manifest freezes the referrer's quota binding (no credit row)",
      ("quota:referral:referrer" in frozen, "credit:referral_reward:referrer" in frozen), (True, False))
check("the code changed owner between T1 and T_final: the new owner is NOT linked", linked != THIEF, True)
check("...and neither the new owner nor the frozen referrer got a reward under that changed identity",
      quotas("volref", "thief"), before)
before = quotas("volref2")
frozen, linked = volume_signup(4, "volnew2", 1502, "VOL2", ok_runner)
check("volume-only reward, code unchanged: the frozen referrer is linked and gets exactly 1 GB",
      (linked, quotas("volref2")[0] - before[0]), (VOLREF2, GB))

# ------------------------------------------------------------------ only the new customer's reward (#1099)
with SessionLocal() as db:
    row = db.get(models.PanelSettings, 1)
    row.referral_referrer_reward_credit, row.referral_referrer_reward_gb = 0, 0
    row.referral_new_user_reward_credit, row.referral_new_user_reward_gb = 30, 0
    db.commit()
    ops_before = db.query(mp.ProvisioningOperation).count()


def new_user_only(local_id, username, telegram_id, legacy_manifest=False):
    approval_uuid, token = register(ri.ApprovalIntent(
        pending_source_instance_id="b", pending_local_id=local_id, kind="new", target_username=username, amount=1000,
        package_id=PACKAGE, referral_code="VOL2", list_price=1000, claimed_telegram_id=telegram_id))
    expected = rv.receipt_approval_expected_effects
    with SessionLocal() as db:
        if legacy_manifest:
            # registered before the identity row existed: neither the rows nor
            # the registered hash have it
            db.execute(expected.delete().where(expected.c.approval_uuid == approval_uuid,
                                               expected.c.effect_key == "credit:referral_reward:referrer"))
            rows = [ri.ManifestRow(r["effect_type"], r["effect_key"], r["requirement"], json.loads(r["expected"]))
                    for r in db.execute(select(expected).where(expected.c.approval_uuid == approval_uuid)).mappings()]
            db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == approval_uuid)
                       .values(manifest_hash=ri.manifest_hash(rows)))
            db.commit()
        frozen = {r["effect_key"]: (r["requirement"], json.loads(r["expected"])) for r in db.execute(
            select(expected).where(expected.c.approval_uuid == approval_uuid)).mappings() if "referral" in r["effect_key"]}
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
        answer = code(lambda: bot_router.create_user(schemas.BotCreateUserRequest(
            username=username, telegram_id=telegram_id, package_id=PACKAGE, connections=[
                schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")], paid_amount=1000,
            payment_method="card", approval_uuid=approval_uuid, execution_token=token, referral_code="VOL2",
            list_price=1000), db=db, principal=INTERNAL))
    with SessionLocal() as db:
        state = db.execute(select(rv.receipt_approvals.c.state).where(
            rv.receipt_approvals.c.approval_uuid == approval_uuid)).scalar()
        user = db.query(models.User).filter_by(username=username).one_or_none()
        return frozen, answer, state, user and (user.referred_by_id, int(user.balance or 0))


referrer_before = quotas("volref2")
frozen, answer, state, linked = new_user_only(5, "lonely", 1503)
check("manifest with only the new customer's reward also freezes the referrer, as a zero optional row",
      frozen, {"credit:referral_reward:new_user": ("required", {"user": {"ref": "effect:user_created:user"},
                                                                "amount": 30}),
               "credit:referral_reward:referrer": ("optional", {"referrer_user_id": VOLREF2,
                                                                "referrer_username": "volref2", "amount": 0})})
check("...the durable execution completes, linked to that referrer, the new customer credited 30 once",
      (isinstance(answer, tuple), state, linked), (False, "completed", (VOLREF2, 30)))
check("...the referrer is given nothing", quotas("volref2"), referrer_before)
with SessionLocal() as db:
    ops_before = db.query(mp.ProvisioningOperation).count()
frozen, answer, state, linked = new_user_only(6, "lonely2", 1504, legacy_manifest=True)
with SessionLocal() as db:
    ops_after = db.query(mp.ProvisioningOperation).count()
check("a manifest registered before the identity row (no referrer frozen): refused fail-closed before any write",
      (answer, linked, ops_after - ops_before, state), ((409, "referral_referrer_not_frozen"), None, 0, "registered"))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
