"""Receipt Void P9 (wallet lots) and P9a (joint activation, payment-time
loyalty) end to end on the durable machine. Fake runner; no network.

P9  preflight lists a negative balance and stops; the cutover backfills one
    account per customer, a legacy_baseline lot per positive balance and
    copies the purchase lock to the top-up lock, in one transaction;
    a wallet sale holds FIFO over the lots in T1 and captures with its
    LedgerEntry in T_final; a sale that fails releases the hold (R1);
    a receipt top-up is a receipt_topup credit source of its approval,
    once; without an approval it is refused; a manual adjustment is a
    source or a captured debit with its reason; an overdraft changes
    nothing; the rollback is refused once the lots were used; the
    reconciliation finds nothing wrong.
P9a the activation refuses a reserved renewal and an outdated remote key,
    then switches 'required' + 'payment_event' at once with an epoch and
    baselines (anchor P - P mod T); a wallet sale is counted and earns its
    slot (one credit source); a card sale without approval is refused; a
    'required' approval is counted as card_paid with its effects; a
    reseller-paid panel sale is not counted (decision #16); a threshold
    change opens a new epoch; the deactivation is refused once used; a
    renewal approval names one service (owner decision).
(p6_ready() is patched to ready: this test is about the wallet, and the
real readiness - host ownership, lock verification - is covered by the P6
tests.)
"""
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

DIRECTORY = tempfile.mkdtemp(prefix="um-wallet-p9-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(DIRECTORY, "t.db")
os.environ["UM_LOCK_DIR"] = os.path.join(DIRECTORY, "locks")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _no_network
_no_network.install([])

from fastapi import HTTPException
from sqlalchemy import func, select

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.database import SessionLocal, engine
from app.routers import bot as bot_router, users as users_router
from app.services import bot_auth, loyalty_activation, loyalty_service, provisioning_contracts as contracts
from app.services import provisioning_durable as durable, provisioning_parent_execute as execute
from app.services import provisioning_readiness, provisioning_schema, receipt_void_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import remote_action as action, user_ops, wallet_accounts, wallet_adjustments
from app.services import wallet_cutover, wallet_lots, wallet_reconcile
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def code(call):
    try:
        return call()
    except HTTPException as exc:
        detail = exc.detail
        return exc.status_code, detail if isinstance(detail, str) else detail.get("error")


models.Base.metadata.create_all(engine)
check("schemas ready", (receipt_void_schema.bootstrap(engine, SessionLocal)["ready"],
                        provisioning_schema.bootstrap(engine, SessionLocal)["ready"]), (True, True))
HOST = HostIdentity("a7" * 32, str(uuid.uuid4()))
durable.read_identity = lambda: HOST
with SessionLocal() as db:
    host = db.get(mp.ProvisioningRuntimeState, 1)
    host.owner_state, host.owner_host_id, host.owner_boot_id = "active", HOST.host_id, HOST.boot_id
    host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
    host.ownership_epoch, host.gate_mode, host.lock_backend = 1, "enforced", "flock"
    db.add(models.PanelSettings(id=1, loyalty_purchase_threshold=0, loyalty_reward_credit=0))   # loyalty set before P9a
    # This execution fixture permits auto approvals explicitly; policy denial
    # is covered separately by the central auto-approval tests.
    db.add(models.BotSettings(id=1, auto_approve_enabled=True,
                              auto_approve_ignore_hours=True, auto_approve_returning_only=False))
    root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
    reseller = models.AdminUser(username="res", hashed_password="x", balance=100_000)
    remote_key = models.ApiKey(key="remote-key", label="remote bot", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
    db.add_all([root, reseller, remote_key])
    db.flush()
    node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.24.0.1",
                       mt_wireguard_interface="wg0", mt_client_subnet="10.96.0.0/24")
    db.add(node)
    db.flush()
    db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
        adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
        server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
        verified_at=dt.datetime.utcnow()))
    db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
    shared = models.Package(name="S", quota_gb=10, duration_days=30, price=400)
    panel_pkg = models.Package(name="R", quota_gb=5, duration_days=30, price=400, cooperation_price=300,
                               owner_admin_id=reseller.id)
    db.add_all([shared, panel_pkg])
    db.flush()
    for item in (shared, panel_pkg):
        db.add(models.PackageConnection(package_id=item.id, node_id=node.id, protocol="wireguard"))
    for name, tg, balance in (("cust", 3001, 700), ("poor", 3002, 0), ("locked", 3003, 50), ("neg", 3004, -10),
                              ("card", 3005, 0)):
        user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg, balance=balance)
        if name == "locked":
            user.purchases_blocked, user.purchases_blocked_reason = True, "fraud"
        if name == "cust":
            user.purchase_count = 3
    db.commit()
    SHARED, PANEL, NODE, RESELLER, ROOT, KEY = shared.id, panel_pkg.id, node.id, reseller.id, root.id, remote_key.id
    db.execute(rv.receipt_approval_runtime_state.update().values(registration_mode="shadow", key_identity_ready=True,
                                                                 key_identity_failure=None))
    for kind in mp.OPERATION_TYPES:
        db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
    db.commit()
PRINCIPAL = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
NO_SHARE = patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node"))
READY = patch.object(provisioning_readiness, "p6_ready", return_value=(True, []))


def ok_runner(dto):
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def refusing_runner(dto):
    """The node is unreachable and the operation can no longer roll forward:
    compensation (nothing was written)."""
    if dto.action_type is action.ActionType.WG_ENSURE_PRESENT:
        with SessionLocal() as db:
            db.query(mp.ProvisioningOperation).filter(mp.ProvisioningOperation.state.notin_(
                ("completed", "compensated"))).update({"forward_deadline": dt.datetime(2000, 1, 1)})
            db.commit()
        return action.RemoteActionResult(dto.action_id, action.Outcome.TRANSPORT_ERROR, write_attempted=False,
                                         error_code="connect_failed")
    return ok_runner(dto)


def balance(name):
    with SessionLocal() as db:
        return int(db.query(models.User).filter_by(username=name).one().balance or 0)


def lots(name):
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(username=name).one()
        account = db.execute(select(rv.wallet_accounts.c.id).where(rv.wallet_accounts.c.user_id == user.id)).scalar()
        return [(r.source_kind, int(r.available_amount), r.state) for r in db.execute(select(rv.wallet_lots).where(
            rv.wallet_lots.c.wallet_account_id == account).order_by(rv.wallet_lots.c.id))]


def wallet_sale(name, key, runner=ok_runner, price=400):
    with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=runner):
        return code(lambda: len(bot_router.purchase_package(name, schemas.BotPurchasePackageRequest(
            package_id=SHARED, paid_amount=price, payment_method="wallet", charge_wallet=True), db=db,
            owner_admin_id=None, principal=PRINCIPAL, x_idempotency_key=key).connections))


# ====================================================================== P9: preflight and cutover
with SessionLocal() as db, READY:
    report = wallet_cutover.preflight(db)
check("preflight: a negative balance stops the cutover, listed", (report["ready"], [b["code"] for b in report["blockers"]],
      report["blockers"][0].get("items")), (False, ["negative_balances"], [{"user_id": 4, "username": "neg"}]))
with SessionLocal() as db, READY:
    refused = code(lambda: wallet_cutover.cutover(db))
    phase = db.execute(select(rv.wallet_runtime_state.c.phase)).scalar()
check("cutover with a blocker: refused, the phase stays 'normal'", (refused, phase), ((409, "wallet_cutover_blocked"), "normal"))
with SessionLocal() as db:                 # the owner decided this case by hand (decision #7)
    db.query(models.User).filter_by(username="neg").update({"balance": 0})
    db.commit()
with SessionLocal() as db, READY:
    result = wallet_cutover.cutover(db)
check("cutover: enforced, one baseline lot per positive balance", (result["phase"], result["baseline_lots"]), ("enforced", 2))
check("...the baseline lot is exactly the old balance", (lots("cust"), balance("cust")), ([("legacy_baseline", 700, "active")], 700))
with SessionLocal() as db:
    accounts = db.execute(select(func.count()).select_from(rv.wallet_accounts).where(
        rv.wallet_accounts.c.user_id.isnot(None))).scalar_one()
    users = db.query(models.User).count()
    locked = db.execute(select(rv.wallet_accounts.c.topup_blocked).select_from(rv.wallet_accounts.join(
        models.User.__table__, models.User.__table__.c.id == rv.wallet_accounts.c.user_id)).where(
            models.User.__table__.c.username == "locked")).scalar()
check("...one live account per customer; the purchase lock copied to the top-up lock", (accounts, users, locked),
      (users, users, True))

# ====================================================================== P9: wallet sale, failure, overdraft
key = str(uuid.uuid4())
check("wallet sale: provisioned", wallet_sale("cust", key), 1)
with SessionLocal() as db:
    debit = db.execute(select(rv.wallet_debit_events)).mappings().one()
    sale = db.query(models.LedgerEntry).filter_by(kind="sale_new").one()
check("...held in T1, captured with its LedgerEntry in T_final", (debit["state"], debit["ledger_entry_id"], debit["amount"]),
      ("captured", sale.id, 400))
check("...taken FIFO from the lots; the cache follows", (lots("cust"), balance("cust")),
      ([("legacy_baseline", 300, "active")], 300))
check("the same key again: the same sale, nothing taken twice", (wallet_sale("cust", key), balance("cust")), (1, 300))
check("a wallet sale the balance does not cover: 400, nothing taken", (wallet_sale("poor", str(uuid.uuid4())), balance("poor")),
      ((400, "موجودی کیف پول کافی نیست"), 0))
failed = wallet_sale("cust", str(uuid.uuid4()), runner=refusing_runner, price=200)
with SessionLocal() as db:
    states = sorted(r.state for r in db.execute(select(rv.wallet_debit_events.c.state)))
check("a sale whose remote step failed: the hold is released (R1), the money is back",
      (isinstance(failed, tuple), states, balance("cust"), lots("cust")),
      (True, ["captured", "released"], 300, [("legacy_baseline", 300, "active")]))

# ====================================================================== P9: top-ups and adjustments
with SessionLocal() as db:
    approval = registration.register(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="p9", pending_local_id=1, kind="topup", target_username="poor", amount=1500,
        list_price=1500), approval_mode="manual", approved_by_telegram_id=1000)
    db.commit()


def topup(approval_uuid, amount=1500):
    with SessionLocal() as db:
        return code(lambda: bot_router.add_balance("poor", schemas.BotAddBalanceRequest(
            amount=amount, source_kind="receipt_topup", approval_uuid=approval_uuid), db=db, principal=PRINCIPAL).balance)


check("receipt top-up with its approval: a receipt_topup credit source and lot", (topup(approval.approval_uuid), lots("poor")),
      (1500, [("receipt_topup", 1500, "active")]))
check("...again: credited once", (topup(approval.approval_uuid), balance("poor")), (1500, 1500))
check("a top-up with no approval is refused in the lot generation", topup(None, 10), (422, "topup_requires_approval"))
with SessionLocal() as db:
    source = db.execute(select(rv.wallet_credit_sources)).mappings().one()
check("...the source names its approval", (source["origin_kind"], source["causation_approval_uuid"], source["state"]),
      ("receipt_topup", approval.approval_uuid, "applied"))


# ---- a credit while the identity owes (a debt from a void, simulated here): the source settles it first
with SessionLocal() as db:
    poor = db.query(models.User).filter_by(username="poor").one()
    account = wallet_lots.account_of_user(db, poor.id)
    void_op = db.execute(rv.wallet_operations.insert().values(
        operation_type="void_receipt", business_key=str(uuid.uuid4()), request_hash="0" * 64, tenant_scope_key="shared",
        actor_kind="system", state="completed", epoch_at_start=1, result_snapshot="{}", attempts=1,
        created_at=dt.datetime.utcnow(), version=0)).inserted_primary_key[0]
    scam_lot = db.execute(rv.wallet_lots.insert().values(
        wallet_account_id=account["id"], source_kind="legacy_baseline", source_key="scam", original_amount=300,
        available_amount=0, consumed_amount=300, voided_available_amount=0, state="voided",
        voided_at=dt.datetime.utcnow(), void_operation_id=void_op, created_at=dt.datetime.utcnow(),
        version=0)).inserted_primary_key[0]
    db.execute(rv.wallet_debts.insert().values(
        customer_identity_id=account["customer_identity_id"], wallet_account_id=account["id"],
        source_wallet_lot_id=scam_lot, source_void_operation_id=void_op, amount=300, adjusted_amount=0,
        settled_amount=0, state="open", created_at=dt.datetime.utcnow(), version=0))
    owed = registration.register(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="p9", pending_local_id=11, kind="topup", target_username="poor", amount=500,
        list_price=500), approval_mode="manual", approved_by_telegram_id=1000)
    db.commit()
before = balance("poor")
check("a top-up of 500 while 300 is owed: the debt is settled first, the balance grows by 200",
      (topup(owed.approval_uuid, 500), balance("poor") - before), (before + 200, 200))
with SessionLocal() as db:
    effect = db.execute(select(rv.receipt_approval_effects.c.resource_type, rv.receipt_approval_effects.c.delta_value)
                        .where(rv.receipt_approval_effects.c.approval_uuid == owed.approval_uuid,
                               rv.receipt_approval_effects.c.effect_type == "wallet_credit_source_created")).one()
    debt_state = db.execute(select(rv.wallet_debts.c.state, rv.wallet_debts.c.settled_amount)).one()
check("...its effect is the credit source (500), not the balance growth (200); the debt settled",
      (tuple(effect), tuple(debt_state)), (("WalletCreditSource", 500), ("settled", 300)))


def adjust(name, amount, key=None):
    with SessionLocal() as db:
        user = db.query(models.User).filter_by(username=name).one()
        return code(lambda: wallet_adjustments.adjust(db, user.id, amount, actor=db.get(models.AdminUser, ROOT),
                                                      reason="goodwill", token=key or uuid.uuid4()))


check("manual +200: a manual_adjustment source", (adjust("poor", 200), lots("poor")[-1]), (1900, ("manual_adjustment", 200, "active")))
check("manual -1800: a captured manual debit, FIFO", (adjust("poor", -1800), lots("poor")),
      (100, [("receipt_topup", 0, "exhausted"), ("legacy_baseline", 0, "voided"),
             ("debt_settlement_remainder", 0, "exhausted"), ("manual_adjustment", 100, "active")]))
check("an overdraft adjustment: 400, nothing changed", (adjust("poor", -101), balance("poor")),
      ((400, "موجودی کیف پول کافی نیست"), 100))
with SessionLocal() as db:
    check("the rollback is refused once the lots were used", code(lambda: wallet_cutover.rollback(db))[0], 409)
    check("the reconciliation finds nothing wrong", wallet_reconcile.check(db)["problems"], [])

# ====================================================================== P9a: the joint activation
with SessionLocal() as db:
    db.query(models.User).filter_by(username="card").update({"reserved_duration_days": 30})
    db.query(models.PanelSettings).update({"loyalty_purchase_threshold": 2, "loyalty_reward_credit": 300})
    db.commit()
with SessionLocal() as db, READY:
    blocked = code(lambda: loyalty_activation.activate(db, ROOT))
check("activation with a reserved renewal: refused (loyalty_cutover_blocked)", blocked, (409, "loyalty_cutover_blocked"))
with SessionLocal() as db:
    db.query(models.User).filter_by(username="card").update({"reserved_duration_days": None})
    db.commit()
with SessionLocal() as db, READY:
    unconfirmed = code(lambda: loyalty_activation.activate(db, ROOT))
check("activation before the Product decisions were confirmed: refused", unconfirmed,
      (409, "product_decisions_unconfirmed"))
with SessionLocal() as db:
    from app.services import product_decisions
    check("a confirmation must name the exact version and hash",
          code(lambda: product_decisions.confirm(db, ROOT, version=product_decisions.VERSION, content_hash="0" * 64)),
          (409, "product_decisions_version_mismatch"))
    product_decisions.confirm(db, ROOT, version=product_decisions.VERSION, content_hash=product_decisions.CONTENT_HASH)
with SessionLocal() as db, READY:
    outdated = code(lambda: loyalty_activation.activate(db, ROOT))
check("activation with a remote key that never reported the protocol: refused", outdated,
      (409, "approval_protocol_outdated"))
with SessionLocal() as db:
    remote = bot_auth.BotPrincipal.from_api_key(db.get(models.ApiKey, KEY))
    registration._record_protocol_version(db, remote, None)            # an old bot reports nothing
    db.commit()
    unreported = db.get(models.ApiKey, KEY).approval_protocol_version
    registration._record_protocol_version(db, remote, registration.APPROVAL_PROTOCOL_VERSION)
    db.commit()
    reported = db.get(models.ApiKey, KEY).approval_protocol_version
check("a key's protocol version is what its bot reported (nothing from an old bot)", (unreported, reported),
      (None, registration.APPROVAL_PROTOCOL_VERSION))
with SessionLocal() as db, READY:
    activated = loyalty_activation.activate(db, ROOT)
    state = loyalty_service.runtime_row(db)
    anchor = db.execute(select(rv.loyalty_user_epoch_anchors.c.starts_after_active_count).where(
        rv.loyalty_user_epoch_anchors.c.user_id == 1)).scalar()
check("activation: 'required' + 'payment_event' at once, generation 1, cust (P=4) anchored at P - P mod T",
      (activated["activated"], state["registration_mode"], state["loyalty_timing_mode"], state["completeness_generation"],
       anchor), (True, "required", "payment_event", 1, 4))
with SessionLocal() as db, READY:
    check("activation again: 200, no second generation", loyalty_activation.activate(db, ROOT)["already_active"], True)

# ====================================================================== P9a: payment-time loyalty
adjust("cust", 1000)
before = balance("cust")
check("wallet sale after the activation (N=5: no slot yet)", wallet_sale("cust", str(uuid.uuid4()), price=200), 1)
with SessionLocal() as db:
    rewarded = db.execute(select(func.count()).select_from(rv.loyalty_reward_events)).scalar_one()
check("...counted, nothing earned", (rewarded, balance("cust")), (0, before - 200))
check("a second wallet sale (N=6)", wallet_sale("cust", str(uuid.uuid4()), price=200), 1)
with SessionLocal() as db:
    events = [r.eligibility_reason for r in db.execute(select(rv.loyalty_purchase_events))]
    reward = db.execute(select(rv.loyalty_reward_events)).mappings().one()
    cust = db.query(models.User).filter_by(username="cust").one()
check("...counted as wallet_captured; anchor 4, threshold 2 -> slot 1 (required 6), its own credit source",
      (events, reward["slot"], reward["required_active_count"], reward["credit_amount"],
       reward["wallet_credit_source_id"] is not None), (["wallet_captured", "wallet_captured"], 1, 6, 300, True))
check("...the caches follow the events: purchase_count 6, one live reward, the reward credited",
      (cust.purchase_count, cust.loyalty_rewards_given, int(cust.balance), lots("cust")[-1]),
      (6, 1, before - 400 + 300, ("loyalty_reward", 300, "active")))
with SessionLocal() as db, NO_SHARE:
    check("a card sale without an approval under 'required': refused before anything",
          code(lambda: bot_router.purchase_package("card", schemas.BotPurchasePackageRequest(
              package_id=SHARED, paid_amount=400, payment_method="card"), db=db, owner_admin_id=None,
              principal=PRINCIPAL)), (422, "approval_uuid_required"))
with SessionLocal() as db, NO_SHARE:
    check("...also with no payment_method and no paid_amount (an old bot; the package's price applies): refused",
          code(lambda: bot_router.purchase_package("card", schemas.BotPurchasePackageRequest(
              package_id=SHARED), db=db, owner_admin_id=None, principal=PRINCIPAL)), (422, "approval_uuid_required"))
    check("...and a priced new account without evidence: refused before anything",
          code(lambda: bot_router.create_user(schemas.BotCreateUserRequest(
              username="ghost", telegram_id=3999, package_id=SHARED), db=db, principal=PRINCIPAL)),
          (422, "approval_uuid_required"))
with SessionLocal() as db:
    check("...no account was made", db.query(models.User).filter_by(username="ghost").count(), 0)
with SessionLocal() as db:
    answer = registration.begin(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="p9", pending_local_id=2, kind="new", target_username="card", amount=400,
        package_id=SHARED, list_price=400), approval_mode="auto")
    manifest = sorted(r["effect_key"] for r in registration.manifest_of(db, answer["approval_uuid"]))
check("a 'required' registration: its manifest counts the purchase", (answer["mode"], "loyalty_purchase" in manifest),
      ("required", True))
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
    bot_router.purchase_package("card", schemas.BotPurchasePackageRequest(
        package_id=SHARED, paid_amount=400, payment_method="card", approval_uuid=answer["approval_uuid"],
        execution_token=answer["execution_token"], list_price=400), db=db, owner_admin_id=None, principal=PRINCIPAL)
with SessionLocal() as db:
    approval_state = db.execute(select(rv.receipt_approvals.c.state).where(
        rv.receipt_approvals.c.approval_uuid == answer["approval_uuid"])).scalar()
    counted = db.execute(select(rv.loyalty_purchase_events.c.eligibility_reason).where(
        rv.loyalty_purchase_events.c.approval_uuid == answer["approval_uuid"])).scalar()
    effect = db.execute(select(func.count()).select_from(rv.receipt_approval_effects).where(
        rv.receipt_approval_effects.c.approval_uuid == answer["approval_uuid"],
        rv.receipt_approval_effects.c.effect_type == "loyalty_purchase_recorded")).scalar_one()
check("...executed: completed, counted card_paid, its effect recorded", (approval_state, counted, effect),
      ("completed", "card_paid", 1))
with SessionLocal() as db:                 # card's second paid purchase (N=2): a renewal of its one service
    renewal = registration.begin(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="p9", pending_local_id=5, kind="renew", target_username="card", amount=400,
        package_id=SHARED, list_price=400), approval_mode="auto")
    manifest = sorted(r["effect_key"] for r in registration.manifest_of(db, renewal["approval_uuid"]))
check("a renewal that will earn a slot: the manifest projects its loyalty credit",
      (renewal["mode"], renewal["renew_purchase_id"] is not None, any(k.startswith("credit:loyalty:") for k in manifest)),
      ("required", True, True))
with SessionLocal() as db, NO_SHARE:
    bot_router.renew_service("card", renewal["renew_purchase_id"], schemas.BotRenewRequest(
        add_gb=10, add_days=30, package_id=SHARED, paid_amount=400, payment_method="card",
        approval_uuid=renewal["approval_uuid"], execution_token=renewal["execution_token"], list_price=400),
        db=db, owner_admin_id=None, principal=PRINCIPAL)
with SessionLocal() as db:
    state = db.execute(select(rv.receipt_approvals.c.state).where(
        rv.receipt_approvals.c.approval_uuid == renewal["approval_uuid"])).scalar()
    credit_effect = db.execute(select(rv.receipt_approval_effects.c.delta_value).where(
        rv.receipt_approval_effects.c.approval_uuid == renewal["approval_uuid"],
        rv.receipt_approval_effects.c.effect_key.like("credit:loyalty:%"))).scalar()
    reward = db.execute(select(rv.loyalty_reward_events).where(
        rv.loyalty_reward_events.c.causation_approval_uuid == renewal["approval_uuid"])).mappings().one()
check("...executed: completed, the slot's credit effect recorded (300), the reward caused by the approval",
      (state, credit_effect, reward["causation_type"], balance("card")), ("completed", 300, "receipt_approval", 300))
with SessionLocal() as db, NO_SHARE, patch.object(execute.remote_runner, "run_action", side_effect=ok_runner), \
        patch.object(users_router, "_get_scoped_package", side_effect=lambda d, a, i: d.get(models.Package, i)):
    admin = db.get(models.AdminUser, RESELLER)
    users_router.create_user(schemas.UserCreate(username="panelsale", package_id=PANEL), db=db, admin=admin, _perm=None,
                             x_idempotency_key=str(uuid.uuid4()))
with SessionLocal() as db:
    panel_user = db.query(models.User).filter_by(username="panelsale").one()
    events = db.execute(select(func.count()).select_from(rv.loyalty_purchase_events).where(
        rv.loyalty_purchase_events.c.user_id == panel_user.id)).scalar_one()
check("a panel sale paid by the reseller's credit is not counted (decision #16); its cache is 0",
      (events, panel_user.purchase_count), (0, 0))
with SessionLocal() as db:
    epoch = loyalty_service.change_policy(db, ROOT, loyalty_purchase_threshold=5)
    db.commit()
    rows = db.execute(select(rv.loyalty_policy_epochs.c.threshold, rv.loyalty_policy_epochs.c.closed_at.is_(None))
                      .order_by(rv.loyalty_policy_epochs.c.id)).all()
check("a threshold change: the epoch closes, a new one opens (decision #18)", [(t, bool(o)) for t, o in rows],
      [(2, False), (5, True)])
with SessionLocal() as db:
    check("the deactivation is refused once this generation was used", code(lambda: loyalty_activation.deactivate(db)),
          (409, "activation_irreversible"))
    check("the reconciliation finds nothing wrong after the activation", wallet_reconcile.check(db)["problems"], [])

# ====================================================================== renewal approvals: one service
with SessionLocal() as db:
    cust_id = db.query(models.User).filter_by(username="cust").one().id
    services = db.query(models.Purchase).filter_by(user_id=cust_id).count()
    try:
        registration.register(db, PRINCIPAL, ri.ApprovalIntent(
            pending_source_instance_id="p9", pending_local_id=3, kind="renew", target_username="cust", amount=400,
            package_id=SHARED, list_price=400), approval_mode="auto")
        refused = "registered"
    except registration.RegistrationRejected as exc:
        refused = (exc.status, exc.code)
    db.rollback()
check("a renewal approval without its service, for a customer with several: refused",
      (services > 1, refused), (True, (409, "renew_service_selection_required")))
with SessionLocal() as db:
    one = registration.register(db, PRINCIPAL, ri.ApprovalIntent(
        pending_source_instance_id="p9", pending_local_id=4, kind="renew", target_username="card", amount=400,
        package_id=SHARED, list_price=400), approval_mode="auto")
    shape = db.execute(select(rv.receipt_approvals.c.target_shape).where(
        rv.receipt_approvals.c.approval_uuid == one.approval_uuid)).scalar()
    db.rollback()
check("...for a customer with exactly one: registered as that service's renewal", shape, "renew_purchase")

check("no network", _no_network.attempts, [])
engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
