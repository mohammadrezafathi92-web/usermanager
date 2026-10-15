"""Receipt approvals through the durable path from the REAL bot endpoints
(Lifecycle 7.6, 7.7). Fake runner; no network.

  * shadow, new customer with a bundled package: one operation
    approval:{uuid}:v:{token}; the approval completed with exactly its
    manifest's effects; the sale tagged; the bot's separate referral /
    discount calls and finalize answer from the operation, nothing twice;
  * required, existing reseller customer + discount + card: the reseller's
    credit reserved in T1 and captured with ledger_credit_spend; the
    discount redeemed INSIDE T_final (optional effect) and the bot's later
    redeem answered idempotently; the card counted once; legacy not used;
  * required, renew_purchase: renewal, sale, purchase_renewed in one commit;
  * an old token -> 409 approval_superseded, no operation; no token -> 422;
  * a changed intent (another amount) -> 409 approval_intent_changed, the
    approval still registered; a username taken before T1 -> 409 and the
    approval cancelled (username_taken); a mode switched since registration
    -> cancelled (approval_mode_changed).
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-durable-approval-")
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
from app.services import receipt_approval_runtime as runtime, remote_action as action, user_ops, wallet_accounts
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
HOST = HostIdentity("d7" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=10_000)
    db.add_all([root, reseller])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.14.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.41.0.0/24")
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
    card = models.PaymentCard(owner_admin_id=reseller.id, card_number="6037000000000000", is_active=True)
    db.add(card)
    db.add(models.DiscountCode(code="OFF10", kind="percent", value=10, enabled=True, owner_admin_id=reseller.id))
    customer = wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=702, owner_admin_id=reseller.id)
    db.commit()
    SHARED, OWNED, NODE, CARD, RESELLER, CUSTOMER = shared.id, owned.id, node.id, card.id, reseller.id, customer.id
    runtime.set_requested_mode(db, registration_mode="shadow")
    db.execute(rv.receipt_approval_runtime_state.update().values(key_identity_ready=True))
    db.commit()
for kind in ("create_user", "purchase", "renew_purchase"):
    with SessionLocal() as db:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))
NO_RECONCILE = patch.object(user_ops, "reconcile_purchase_connections", return_value=None)
SEQ = iter(range(1, 1000))


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


RUNNER = patch.object(execute.remote_runner, "run_action", side_effect=ok_runner)


def required(on=True):
    with SessionLocal() as db:
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="required" if on else "shadow", loyalty_timing_mode="payment_event" if on else
            "legacy_activation", completeness_generation=1))
        db.commit()


def register(intent, mode="shadow"):
    with SessionLocal() as db:
        result = registration.register(db, PRINCIPAL, intent, approval_mode="auto")
        if mode == "required":
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == result.approval_uuid).values(
                    registered_under_mode="required", completeness_generation=1))
        db.commit()
        return result.approval_uuid, result.execution_token


def approval(approval_uuid):
    with SessionLocal() as db:
        row = db.execute(select(rv.receipt_approvals).where(
            rv.receipt_approvals.c.approval_uuid == approval_uuid)).mappings().one()
        effects = sorted((r.effect_type, r.effect_key) for r in db.execute(select(
            rv.receipt_approval_effects.c.effect_type, rv.receipt_approval_effects.c.effect_key).where(
            rv.receipt_approval_effects.c.approval_uuid == approval_uuid)))
        manifest = sorted((r.effect_type, r.effect_key) for r in db.execute(select(
            rv.receipt_approval_expected_effects.c.effect_type, rv.receipt_approval_expected_effects.c.effect_key)
            .where(rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid)))
        operation = db.execute(select(mp.ProvisioningOperation).where(
            mp.ProvisioningOperation.approval_uuid == approval_uuid)).scalar_one_or_none()
        return dict(row), effects, manifest, operation


def spec():
    return [schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")]


def intent(kind, username, amount, package, **extra):
    return ri.ApprovalIntent(pending_source_instance_id="bot-test", pending_local_id=next(SEQ), kind=kind,
                             target_username=username, amount=amount, package_id=package, list_price=extra.pop(
                                 "list_price", amount), **extra)


def code_of(call):
    try:
        call()
        return "ok"
    except HTTPException as exc:
        return exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")


# ------------------------------------------------------------------ A. shadow, new customer
uuid_a, token_a = register(intent("new", "newbie", 1000, SHARED, claimed_telegram_id=701))
with SessionLocal() as db, NO_SHARE, RUNNER:
    out = bot_router.create_user(schemas.BotCreateUserRequest(
        username="newbie", telegram_id=701, package_id=SHARED, quota_gb=10, expire_days=30, connections=spec(),
        paid_amount=1000, payment_method="card", approval_uuid=uuid_a, execution_token=token_a, list_price=1000),
        db=db, principal=PRINCIPAL)
row, effects, manifest, operation = approval(uuid_a)
check("A. shadow new customer: one operation approval:{uuid}:v:{token}, completed",
      (operation.business_key, operation.state, operation.operation_type),
      (f"approval:{uuid_a}:v:{token_a}", "completed", "create_user"))
check("A. the approval completed with exactly its manifest's effects", (row["state"], effects == manifest,
      len(effects)), ("completed", True, 4))
with SessionLocal() as db:
    user = db.query(models.User).filter_by(username="newbie").one()
    sale = db.get(models.LedgerEntry, operation.sale_ledger_entry_id)
    check("A. the customer, one service with its connection, the sale tagged with the approval",
          (out.username, len(user.purchases), len(user.connections), sale.approval_uuid, sale.amount),
          ("newbie", 1, 1, uuid_a, 1000))
    referral = bot_router.apply_referral(schemas.ReferralApplyRequest(
        username="newbie", referral_code="X", approval_uuid=uuid_a, execution_token=token_a), db=db, principal=PRINCIPAL)
    final = registration.finalize(db, PRINCIPAL, uuid_a, reported_failure=True)
    db.commit()
check("A. the bot's separate referral call is answered from the operation (not requested: not applied)",
      referral, {"ok": False, "reason": "not_requested"})
check("A. finalize (even reporting a failure) only reads the completed state back",
      (final["state"], final["operation_state"], approval(uuid_a)[0]["state"]), ("completed", "completed", "completed"))

# ------------------------------------------------------------------ B. required, reseller customer, discount + card
required()
uuid_b, token_b = register(intent("new", "cust", 450, OWNED, payment_card_id=CARD, discount_code="off10",
                                  list_price=500, claimed_telegram_id=702), mode="required")
with SessionLocal() as db:
    balance_before = db.get(models.AdminUser, RESELLER).balance
legacy = patch.object(user_ops, "apply_package_as_purchase", side_effect=AssertionError("legacy path used"))
with SessionLocal() as db, NO_SHARE, RUNNER, legacy:
    bought = bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
        package_id=OWNED, connections=spec(), paid_amount=450, payment_method="card", payment_card_id=CARD,
        discount_code="off10", approval_uuid=uuid_b, execution_token=token_b, list_price=500),
        db=db, owner_admin_id=None, principal=PRINCIPAL)
row, effects, manifest, operation = approval(uuid_b)
with SessionLocal() as db:
    reservation = db.query(mp.PaymentReservation).filter_by(operation_id=operation.id).one()
    used = db.query(models.DiscountCode).filter_by(code="OFF10").one().used_count
    spent = balance_before - db.get(models.AdminUser, RESELLER).balance
    accumulated = db.get(models.PaymentCard, CARD).accumulated_amount
check("B. required purchase: completed, approval completed with every manifest effect",
      (operation.state, row["state"], effects == manifest), ("completed", "completed", True))
check("B. the manifest's credit spend and the optional discount are among the recorded effects",
      (("ledger_credit_spend", "credit_spend") in effects, any(k == "discount_redeemed" for k, _ in effects)), (True, True))
check("B. the reseller's credit reserved in T1 and captured once (300); the discount used once; card counted once",
      (reservation.payer_kind, reservation.state, spent, used, accumulated, len(bought.connections)),
      ("reseller_credit", "captured", 300, 1, 450, 1))
with SessionLocal() as db:
    redeem = bot_router.redeem_discount(schemas.DiscountRedeemRequest(
        code="off10", username="cust", package_price=500, approval_uuid=uuid_b, execution_token=token_b),
        db=db, principal=PRINCIPAL)
    card_answer = bot_router.record_payment_card_use(CARD, schemas.BotRecordCardPaymentRequest(
        amount=450, approval_uuid=uuid_b, execution_token=token_b), db=db, principal=PRINCIPAL)
with SessionLocal() as db:
    check("B. the bot's later redeem and record-payment: answered from T_final, nothing applied twice",
          (redeem.valid, redeem.discount_amount, redeem.final_price, card_answer,
           db.query(models.DiscountCode).filter_by(code="OFF10").one().used_count,
           db.get(models.PaymentCard, CARD).accumulated_amount), (True, 50, 450, {"ok": True}, 1, 450))
    PURCHASE_B = db.query(models.Purchase).filter_by(user_id=CUSTOMER).one().id

# ------------------------------------------------------------------ C. required, renewal
uuid_c, token_c = register(intent("renew", "cust", 500, OWNED, renew_purchase_id=PURCHASE_B,
                                  claimed_telegram_id=702), mode="required")
with SessionLocal() as db, NO_SHARE, NO_RECONCILE:
    bot_router.renew_service("cust", PURCHASE_B, schemas.BotRenewRequest(
        package_id=OWNED, add_gb=5, add_days=30, paid_amount=500, payment_method="card", approval_uuid=uuid_c,
        execution_token=token_c, list_price=500), db=db, owner_admin_id=None, principal=PRINCIPAL)
row, effects, manifest, operation = approval(uuid_c)
check("C. required renewal: one transaction, approval completed with purchase_renewed and the sale",
      (operation.operation_type, operation.state, row["state"], effects == manifest,
       ("purchase_renewed", "renew") in effects), ("renew_purchase", "completed", "completed", True, True))

# ------------------------------------------------------------------ D/E. tokens
uuid_d, token_d = register(intent("new", "cust", 500, OWNED, claimed_telegram_id=702), mode="required")
with SessionLocal() as db:
    db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == uuid_d)
               .values(version=rv.receipt_approvals.c.version + 1))
    db.commit()


def buy_d(token, amount=500):
    with SessionLocal() as db, NO_SHARE, RUNNER:
        return bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
            package_id=OWNED, connections=spec(), paid_amount=amount, payment_method="card", approval_uuid=uuid_d,
            execution_token=token, list_price=500), db=db, owner_admin_id=None, principal=PRINCIPAL)


check("D. the old token after a takeover: 409 approval_superseded, no operation",
      (code_of(lambda: buy_d(token_d)), approval(uuid_d)[3]), ("approval_superseded", None))
check("E. no execution token on a durable approval call: refused before any write",
      (code_of(lambda: buy_d(None)), approval(uuid_d)[3]), ("approval_execution_token_required", None))

# ------------------------------------------------------------------ F/G/H. preconditions
check("F. another amount than registered: 409 approval_intent_changed, the approval still registered",
      (code_of(lambda: buy_d(token_d + 1, amount=400)), approval(uuid_d)[0]["state"], approval(uuid_d)[3]),
      ("approval_intent_changed", "registered", None))
uuid_g, token_g = register(intent("new", "taken", 1000, SHARED, claimed_telegram_id=703), mode="required")
with SessionLocal() as db:
    wallet_accounts.create_user_with_wallet(db, username="taken", telegram_id=704)
    db.commit()


def create_g():
    with SessionLocal() as db, NO_SHARE, RUNNER:
        bot_router.create_user(schemas.BotCreateUserRequest(
            username="taken", telegram_id=703, package_id=SHARED, connections=spec(), paid_amount=1000,
            payment_method="card", approval_uuid=uuid_g, execution_token=token_g, list_price=1000),
            db=db, principal=PRINCIPAL)


# The username is now an existing account: registration's target (new_user)
# no longer describes it, so T1 cancels instead of creating anything.
result_g = code_of(create_g)
row_g = approval(uuid_g)[0]
check("G. a username taken before T1: 409, the approval cancelled with the reason, nothing created",
      (result_g in ("username_taken", "provisioning_username_taken"), row_g["state"], row_g["cancellation_reason"],
       approval(uuid_g)[3]), (True, "cancelled", "username_taken", None))
required(False)                       # effective 'shadow' now; the next one was registered under 'required'
uuid_h, token_h = register(intent("new", "cust", 500, OWNED, claimed_telegram_id=702), mode="required")


def buy_h():
    with SessionLocal() as db, NO_SHARE, RUNNER:
        bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
            package_id=OWNED, connections=spec(), paid_amount=500, payment_method="card", approval_uuid=uuid_h,
            execution_token=token_h, list_price=500), db=db, owner_admin_id=None, principal=PRINCIPAL)


check("H. the mode switched since registration: 409 and the approval cancelled (approval_mode_changed)",
      (code_of(buy_h), approval(uuid_h)[0]["state"], approval(uuid_h)[0]["cancellation_reason"]),
      ("approval_mode_changed", "cancelled", "approval_mode_changed"))

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
