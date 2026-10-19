"""Receipt Void P10 (preview) and P11 (durable execute) end to end, on SQLite
AND real MariaDB (CI). Fake runner; no network beyond the scratch database.

  gates      off by default (404); none before the joint activation; execute
             needs preview and the worker; the worker cannot be switched off
             under execute or with a void running.
  preview    by approval_uuid or ledger row; a ledger row with no approval is
             409 not traceable; shadow approvals 409 not voidable; another
             admin 404; scope, money projections, no credential anywhere; a
             signed expiring token - tampered, expired, stale state 409.
  new_user   the whole account: blocked, every connection removed by the
             durable deletion (verified absent), rows deleted by its
             finalizer, sale voided + receipt_void pair, loyalty event voided,
             card event voided and its counter recomputed, approval voided;
             summary / transactions / export hide it, include_voided shows it.
             The same execute again: the same operation; another reason 409.
  topup      no deletion; fraud hold, never topup_blocked; the spent part a
             debt of the identity (RV-118..), the rest clawed back; the
             healthy wallet sale stays.
  existing   only the purchase it created: the reseller's credit refunded
             (admin_credit_refund + balance), the discount redemption voided
             and used_count recomputed, the user and its old service stay.
  renew      the whole account, with the other approval on it warned.
  ambiguous  an unverified absence: cleanup_required, nothing deleted; retry
             converges; another one forced (orphan recorded) finishes.
  crash      a step that fails is retried from that step; the db_delete
             transaction rolled back leaves every row.
"""
import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
import threading
import time
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-receipt-void-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import accounting as accounting_router, bot as bot_router
from app.services import accounting, bot_auth, loyalty_activation, payment_card_events
from app.services import provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_readiness, provisioning_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_engine as void_engine, receipt_void_flags as flags
from app.services import receipt_void_operator as operator, receipt_void_schema, receipt_void_scope as scope
from app.services import receipt_void_steps as void_steps, remote_action as action, user_ops, wallet_accounts
from app.services import wallet_cutover, wallet_reconcile
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


ABSENT = {"mode": "absent"}


def runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        if ABSENT["mode"] == "unverified":
            # Neither a read nor a second delete can tell "absent" (13.4).
            return action.RemoteActionResult(dto.action_id, action.Outcome.CONFLICT, write_attempted=False,
                                             remote_outcome="unverified")
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


SECRET_KEYS = ("wg_private_key", "wg_preshared_key", "xr_uuid", "ppp_password", "password", "private_key", "psk",
               "execution_token", "subscription_token")


def leaks(document) -> list:
    text = json.dumps(document, default=str)
    return [k for k in SECRET_KEYS if f'"{k}"' in text]


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Session)["ready"],
                                provisioning_schema.bootstrap(engine, Session)["ready"]), (True, True))
    host_identity = HostIdentity(("e1" if label == "sqlite" else "e2") * 32, str(uuid.uuid4()))
    durable.read_identity = lambda: host_identity
    with Session() as db:
        host = db.get(mp.ProvisioningRuntimeState, 1)
        host.owner_state, host.owner_host_id, host.owner_boot_id = "active", host_identity.host_id, host_identity.boot_id
        host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
        host.ownership_epoch, host.gate_mode = 1, "enforced"
        host.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.add(models.PanelSettings(id=1, loyalty_purchase_threshold=0, loyalty_reward_credit=0))
        root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
        reseller = models.AdminUser(username="res", hashed_password="x", balance=5000, telegram_id=1001)
        other = models.AdminUser(username="other", hashed_password="x", balance=0, telegram_id=1002)
        db.add_all([root, reseller, other])
        db.flush()
        node = models.Node(name="mt-void", type=models.NodeType.mikrotik, enabled=True, mt_host="10.18.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.71.0.0/24")
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
        card = models.PaymentCard(card_number="6037990000001234", card_holder="Owner", owner_admin_id=None)
        db.add(card)
        db.add(models.DiscountCode(code="OFF10", kind="percent", value=10, enabled=True, owner_admin_id=reseller.id))
        db.flush()
        payment_card_events.capture_baseline(db, None)
        wallet_accounts.create_user_with_wallet(db, username="victim", telegram_id=7001)
        wallet_accounts.create_user_with_wallet(db, username="resc", telegram_id=7002, owner_admin_id=reseller.id)
        wallet_accounts.create_user_with_wallet(db, username="renewer", telegram_id=7003)
        db.add(models.LedgerEntry(kind="sale_new", amount=50, username_snapshot="legacy"))
        for kind in mp.OPERATION_TYPES:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", key_identity_ready=True, key_identity_failure=None))
        db.commit()
        SHARED, OWNED, NODE, ROOT, RESELLER, OTHER, CARD = (shared.id, owned.id, node.id, root.id, reseller.id,
                                                            other.id, card.id)
        LEGACY_ENTRY = db.query(models.LedgerEntry).filter_by(username_snapshot="legacy").one().id
    internal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
    sequence = iter(range(1, 1000))
    patches = contextlib.ExitStack()
    patches.enter_context(patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")))
    patches.enter_context(patch.object(execute.remote_runner, "run_action", side_effect=runner))
    ready = patch.object(provisioning_readiness, "p6_ready", return_value=(True, []))

    def admin(ident):
        with Session() as db:
            row = db.get(models.AdminUser, ident)
            db.expunge(row)
            return row

    ROOT_ADMIN, OTHER_ADMIN = admin(ROOT), admin(OTHER)

    def begin(kind, username, amount, **extra):
        mode = "manual" if kind == "topup" else "auto"
        intent = ri.ApprovalIntent(pending_source_instance_id=f"void-{label}", pending_local_id=next(sequence),
                                   kind=kind, target_username=username, amount=amount,
                                   list_price=extra.pop("list_price", amount), **extra)
        with Session() as db:
            return registration.begin(db, internal, intent, approval_mode=mode,
                                      approved_by_telegram_id=1000 if mode == "manual" else None)

    def run(call):
        with Session() as db:
            try:
                return call(db)
            except HTTPException as exc:
                return exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")

    def approval(approval_uuid):
        with Session() as db:
            return dict(db.execute(select(rv.receipt_approvals).where(
                rv.receipt_approvals.c.approval_uuid == approval_uuid)).mappings().one())

    def preview(approval_uuid=None, ledger_entry_id=None, who=None):
        with Session() as db:
            return code(lambda: scope.preview(db, who or ROOT_ADMIN, approval_uuid=approval_uuid,
                                              ledger_entry_id=ledger_entry_id))

    def void(token, username, reason="fake receipt", ack=True):
        return code(lambda: void_engine.execute(Session, ROOT_ADMIN, preview_token=token, typed_username=username,
                                                reason=reason, danger_ack=ack, ip="203.0.113.7"))

    def steps_of(operation_id):
        with Session() as db:
            return {s["step_key"]: (s["state"], s["remote_outcome"]) for s in void_engine._steps(db, operation_id)}

    def user_id(name):
        with Session() as db:
            row = db.query(models.User).filter_by(username=name).one_or_none()
            return row.id if row else None

    # ================================================================ gates
    with Session() as db:
        check(L + "gates: all off by default", flags.state(db), {"preview": False, "execute": False, "worker": False})
        check(L + "...an endpoint behind an off flag is 404", code(lambda: flags.require(db, "preview")),
              (404, "receipt_void_disabled"))
        check(L + "...none can be switched on before the joint activation (P9a)",
              code(lambda: flags.set_flag(db, "preview", True)), (409, "receipt_void_requires_joint_activation"))
    with Session() as db, ready:
        check(L + "wallet cutover", wallet_cutover.cutover(db)["phase"], "enforced")
    with Session() as db:
        db.query(models.PanelSettings).update({"loyalty_purchase_threshold": 2, "loyalty_reward_credit": 100})
        db.commit()
    with Session() as db:
        from app.services import product_decisions
        product_decisions.confirm(db, ROOT, version=product_decisions.VERSION, content_hash=product_decisions.CONTENT_HASH)
    with Session() as db, ready:
        check(L + "joint activation", loyalty_activation.activate(db, ROOT)["activated"], True)
    with Session() as db:
        check(L + "execute before preview: refused", code(lambda: flags.set_flag(db, "execute", True)),
              (409, "receipt_void_execute_requires_preview"))
        flags.set_flag(db, "preview", True)
        check(L + "execute without the worker: refused (it would strand every void)",
              code(lambda: flags.set_flag(db, "execute", True)), (409, "receipt_void_execute_requires_worker"))
        flags.set_flag(db, "worker", True)
        check(L + "execute after preview + worker + the reversal index", flags.set_flag(db, "execute", True),
              {"preview": True, "execute": True, "worker": True})
        check(L + "the worker cannot go off while execute is on", code(lambda: flags.set_flag(db, "worker", False)),
              (409, "receipt_void_worker_needed_by_execute"))
        check(L + "preview cannot go off while execute is on", code(lambda: flags.set_flag(db, "preview", False)),
              (409, "receipt_void_preview_needed_by_execute"))

    # ================================================================ new_user: the whole account
    scam = begin("new", "scam1", 1000, package_id=SHARED, payment_card_id=CARD, claimed_telegram_id=7101)
    check(L + "a 'required' new-user approval", scam["mode"], "required")
    created = run(lambda db: len(bot_router.create_user(schemas.BotCreateUserRequest(
        username="scam1", telegram_id=7101, package_id=SHARED, paid_amount=1000, payment_method="card",
        payment_card_id=CARD, approval_uuid=scam["approval_uuid"], execution_token=scam["execution_token"],
        list_price=1000), db=db, principal=internal).connections))
    with Session() as db:
        card_counter = db.get(models.PaymentCard, CARD).accumulated_amount
        sale = db.query(models.LedgerEntry).filter_by(approval_uuid=scam["approval_uuid"], kind="sale_new").one()
        SALE = sale.id
    check(L + "...executed: one connection, the card counter 1000", (created, approval(scam["approval_uuid"])["state"],
                                                                    card_counter), (1, "completed", 1000))
    check(L + "preview: a ledger row with no approval is not traceable (409)", preview(ledger_entry_id=LEGACY_ENTRY),
          (409, "receipt_void_not_traceable"))
    check(L + "preview: another admin's view is 404, never 403", preview(scam["approval_uuid"], who=OTHER_ADMIN),
          (404, "receipt_approval_not_found"))
    check(L + "preview: an unknown approval is 404", preview(str(uuid.uuid4())), (404, "receipt_approval_not_found"))
    shown = preview(ledger_entry_id=SALE)
    check(L + "preview by the ledger row: the approval, whole-account scope, the sale, the card, the loyalty event",
          (shown["approval_uuid"], shown["resources"]["delete"]["kind"], len(shown["resources"]["connections"]),
           shown["resources"]["connections"][0]["node_name"], shown["ledger"]["voidable_sale_amount"],
           shown["ledger"]["card"]["amount"], len(shown["loyalty"]["purchase_events"]), shown["blockers"]),
          (scam["approval_uuid"], "user", 1, "mt-void", 1000, 1000, 1, []))
    check(L + "...no credential, token or secret anywhere in the preview", leaks(shown), [])
    token = shown["preview_token"]
    check(L + "execute: a username typed wrong is 422, nothing written", (void(token, "scam"),
                                                                          approval(scam["approval_uuid"])["state"]),
          ((422, "typed_username_mismatch"), "completed"))
    check(L + "execute: no reason is 422", void(token, "scam1", reason="  "), (422, "reason_required"))
    check(L + "execute: without danger_ack 422", void(token, "scam1", ack=False), (422, "danger_ack_required"))
    check(L + "execute: a tampered token 409", void(token[:-2] + ("00" if token[-2:] != "00" else "11"), "scam1"),
          (409, "preview_token_invalid"))
    with patch.object(scope.time, "time", return_value=scope.time.time() + scope.TOKEN_SECONDS + 5):
        check(L + "execute: an expired preview 409 (the UI reloads it)", void(token, "scam1"), (409, "preview_expired"))
    with Session() as db:                     # the state the preview saw changes (another connection)
        db.execute(rv.receipt_approvals.update().where(
            rv.receipt_approvals.c.approval_uuid == scam["approval_uuid"]).values(version=rv.receipt_approvals.c.version + 1))
        db.commit()
    check(L + "execute: the state changed since the preview 409", void(token, "scam1"), (409, "preview_state_changed"))
    token = preview(scam["approval_uuid"])["preview_token"]
    done = void(token, "scam1")
    op1 = done["operation_id"]
    with Session() as db:
        sale = db.get(models.LedgerEntry, SALE)
        pair = db.query(models.LedgerEntry).filter_by(reversal_of_id=SALE).one()
        effects = db.execute(select(rv.receipt_approval_effects.c.effect_type, rv.receipt_approval_effects.c.reversal_state)
                             .where(rv.receipt_approval_effects.c.approval_uuid == scam["approval_uuid"])).all()
        event_state = db.execute(select(rv.loyalty_purchase_events.c.state).where(
            rv.loyalty_purchase_events.c.approval_uuid == scam["approval_uuid"])).scalar()
        card_event = db.execute(select(rv.payment_card_pool_events.c.voided_at).where(
            rv.payment_card_pool_events.c.approval_uuid == scam["approval_uuid"])).scalar()
        counter = db.get(models.PaymentCard, CARD).accumulated_amount
        deletion = db.execute(select(mp.ProvisioningOperation.operation_type, mp.ProvisioningOperation.state).where(
            mp.ProvisioningOperation.business_key.like(f"%:receipt_void:{op1}"))).one()
        account = db.execute(select(rv.wallet_accounts.c.user_id, rv.wallet_accounts.c.tombstoned_at.isnot(None)).where(
            rv.wallet_accounts.c.user_id_snapshot == approval(scam["approval_uuid"])["target_user_id_snapshot"]
            if False else rv.wallet_accounts.c.username_snapshot == "scam1")).one()
        operation = void_engine._operation(db, op1)
    check(L + "void new_user: completed, approval voided", (done["state"], approval(scam["approval_uuid"])["state"]),
          ("completed", "voided"))
    check(L + "...every step in order, the connection verified absent",
          steps_of(op1), {"claim": ("done", None), "block": ("done", None),
                          f"deprovision:{shown['resources']['connections'][0]['id']}": ("done", None),
                          f"verify:{shown['resources']['connections'][0]['id']}": ("done", "verified_absent"),
                          "db_delete": ("done", None), "loyalty_reconciliation": ("done", None),
                          "wallet_reversal": ("done", None), "ledger_reversal": ("done", None),
                          "card_reversal": ("done", None), "finalize": ("done", None)})
    check(L + "...the account deleted by the durable deletion's finalizer; its wallet account a tombstone",
          (user_id("scam1"), deletion, tuple(account)), (None, ("delete_user", "completed"), (None, True)))
    check(L + "...the sale voided with its receipt_void pair (amount 0); the books keep both",
          (sale.voided_at is not None, sale.void_operation_id, pair.kind, pair.amount, pair.approval_uuid),
          (True, op1, "receipt_void", 0, scam["approval_uuid"]))
    check(L + "...every effect reversed once", sorted({state for _type, state in effects} - {None}) and
          all(state is not None for _type, state in effects), True)
    check(L + "...the loyalty purchase voided; the card event voided, its counter recomputed (0)",
          (event_state, card_event is not None, counter), ("voided", True, 0))
    check(L + "...the operation keeps actor, IP, reason", (operation["actor_id"], operation["actor_ip"],
                                                           operation["reason"]), (ROOT, "203.0.113.7", "fake receipt"))
    check(L + "the same execute again (any actor): the same operation", void(token, "scam1")["operation_id"], op1)
    check(L + "...with another reason for the approval: 409", void(token, "scam1", reason="other"),
          (409, "receipt_void_request_changed"))
    with Session() as db:
        business = accounting_router.list_transactions(db=db, current=ROOT_ADMIN)
        audit_view = accounting_router.list_transactions(include_voided=True, db=db, current=ROOT_ADMIN)
        sold = accounting.scoped_query(db, ROOT_ADMIN).filter(models.LedgerEntry.kind == "sale_new").with_entities(
            func.coalesce(func.sum(models.LedgerEntry.amount), 0)).scalar()
        voided_row = next(i for i in audit_view.items if i.id == SALE)
    check(L + "business view: the voided sale is not a sale (sum = the legacy 50); audit view shows the pair",
          (SALE in [i.id for i in business.items], {SALE, pair.id} <= {i.id for i in audit_view.items}, int(sold),
           voided_row.approval_state), (False, True, 50, "voided"))
    with Session() as db:
        check(L + "preview of a voided approval: 409 not voidable", code(lambda: scope.preview(
            db, ROOT_ADMIN, approval_uuid=scam["approval_uuid"])), (409, "approval_not_voidable_state"))
        audit = operator.audit(db, ROOT_ADMIN, scam["approval_uuid"])
        status = operator.status(db, ROOT_ADMIN, op1)
    check(L + "audit and status: the steps and the pair, no credential", (
        audit["operation"]["state"], len(audit["ledger"]), leaks(audit), leaks(status)), ("completed", 2, [], []))

    # ================================================================ shadow approval: never voidable
    shadow = begin("topup", "victim", 10)
    with Session() as db:
        db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == shadow["approval_uuid"])
                   .values(registered_under_mode="shadow", completeness_generation=0, state="completed"))
        db.commit()
    check(L + "a shadow (pre-activation) approval: 409 approval_not_voidable_generation",
          preview(shadow["approval_uuid"]), (409, "approval_not_voidable_generation"))

    # ================================================================ topup: debt, fraud hold, healthy sale stays
    top = begin("topup", "victim", 1500)
    credited = run(lambda db: bot_router.add_balance("victim", schemas.BotAddBalanceRequest(
        amount=1500, source_kind="receipt_topup", approval_uuid=top["approval_uuid"],
        execution_token=top["execution_token"]), db=db, principal=internal).balance)
    finalized = run(lambda db: (registration.finalize(db, internal, top["approval_uuid"])["state"], db.commit())[0])
    check(L + "a receipt top-up of 1500, finalized by the bot", (credited, finalized), (1500, "completed"))
    bought = run(lambda db: len(bot_router.purchase_package("victim", schemas.BotPurchasePackageRequest(
        package_id=SHARED, paid_amount=1000, payment_method="wallet", charge_wallet=True), db=db,
        owner_admin_id=None, principal=internal, x_idempotency_key=str(uuid.uuid4())).connections))
    check(L + "...1000 of it spent on a healthy wallet sale", bought, 1)
    shown = preview(top["approval_uuid"])
    source = shown["wallet"]["sources"][0]
    check(L + "preview topup: no resource deleted; projected clawback 500, debt 1000; the sale stays valid",
          (shown["resources"]["delete"], source["gross"], source["projected_clawback"], source["projected_debt"],
           source["projected_writeoff"], source["spent_sales_stay_valid"]), (None, 1500, 500, 1000, 0, True))
    done = void(shown["preview_token"], "victim")
    with Session() as db:
        victim = db.query(models.User).filter_by(username="victim").one()
        account = db.execute(select(rv.wallet_accounts).where(rv.wallet_accounts.c.user_id == victim.id)).mappings().one()
        debts = [(int(r.amount), r.state) for r in db.execute(select(rv.wallet_debts).where(
            rv.wallet_debts.c.source_void_operation_id == done["operation_id"]))]
        healthy = db.query(models.LedgerEntry).filter_by(user_id=victim.id, kind="sale_new").one().voided_at
        topup_row = db.query(models.LedgerEntry).filter_by(approval_uuid=top["approval_uuid"],
                                                           kind="wallet_topup").one().voided_at
        hold = (victim.purchases_blocked, victim.purchases_blocked_reason, account["topup_blocked"], victim.balance)
        problems = wallet_reconcile.check(db)["problems"]
    check(L + "void topup: completed without deletion steps", (done["state"], [k for k in steps_of(done["operation_id"])]),
          ("completed", ["claim", "block", "wallet_reversal", "ledger_reversal", "card_reversal", "finalize"]))
    check(L + "...a debt of 1000 for the identity; the rest clawed back (balance 0)", (debts, hold[3]),
          ([(1000, "open")], 0))
    check(L + "...fraud hold coded with the approval, top-ups NOT blocked", hold[:3],
          (True, f"receipt_void:{top['approval_uuid']}", False))
    check(L + "...the healthy sale is untouched; the top-up row voided", (healthy, topup_row is not None),
          (None, True))
    check(L + "...wallet invariants hold (reconciliation)", problems, [])

    # ================================================================ existing_user: only its purchase
    with Session() as db:
        before_res = db.get(models.AdminUser, RESELLER).balance
    old = begin("new", "resc", 500, package_id=OWNED, claimed_telegram_id=7002)
    run(lambda db: bot_router.purchase_package("resc", schemas.BotPurchasePackageRequest(
        package_id=OWNED, paid_amount=500, payment_method="card", approval_uuid=old["approval_uuid"],
        execution_token=old["execution_token"], list_price=500), db=db, owner_admin_id=None, principal=internal))
    second = begin("new", "resc", 450, package_id=OWNED, discount_code="off10", list_price=500,
                   claimed_telegram_id=7002)
    run(lambda db: bot_router.purchase_package("resc", schemas.BotPurchasePackageRequest(
        package_id=OWNED, paid_amount=450, payment_method="card", discount_code="off10",
        approval_uuid=second["approval_uuid"], execution_token=second["execution_token"], list_price=500),
        db=db, owner_admin_id=None, principal=internal))
    with Session() as db:
        spent = before_res - db.get(models.AdminUser, RESELLER).balance
        used = db.query(models.DiscountCode).filter_by(code="OFF10").one().used_count
        purchases_before = db.query(models.Purchase).filter_by(user_id=user_id("resc")).count()
    check(L + "two purchases of a reseller's customer: credit 600 spent, the discount used once",
          (approval(second["approval_uuid"])["state"], spent, used, purchases_before), ("completed", 600, 1, 2))
    shown = preview(second["approval_uuid"])
    check(L + "preview existing_user: only the created purchase; the reseller refund and the discount listed",
          (shown["resources"]["delete"]["kind"], len(shown["resources"]["purchases"]),
           shown["ledger"]["reseller_credit_refund"]["amount"], shown["ledger"]["discount"] is not None),
          ("purchase", 1, 300, True))
    done = void(shown["preview_token"], "resc")
    with Session() as db:
        refund = db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund",
                                                        approval_uuid=second["approval_uuid"]).one()
        res_balance = db.get(models.AdminUser, RESELLER).balance
        used = db.query(models.DiscountCode).filter_by(code="OFF10").one().used_count
        resc = db.query(models.User).filter_by(username="resc").one()
        purchases_after = db.query(models.Purchase).filter_by(user_id=resc.id).count()
        first_sale = db.query(models.LedgerEntry).filter_by(approval_uuid=old["approval_uuid"], kind="sale_new").one()
    check(L + "void existing_user: completed; one purchase gone, the user and the other service stay",
          (done["state"], purchases_after, resc.status, resc.purchases_blocked), ("completed", 1,
                                                                                  models.UserStatus.active, True))
    check(L + "...the reseller credit refunded (admin_credit_refund, balance back), spend row not voided",
          (refund.amount, refund.reversal_of_id is not None, before_res - res_balance), (300, True, 300))
    check(L + "...the discount redemption voided, used_count recomputed (0)", used, 0)
    check(L + "...the other approval's sale untouched", (first_sale.voided_at, approval(old["approval_uuid"])["state"]),
          (None, "completed"))

    # ================================================================ loyalty: entitlement after a void (14.2)
    with Session() as db:
        wallet_accounts.create_user_with_wallet(db, username="loyal", telegram_id=7004)
        db.commit()
    bought = []
    for _ in range(4):                       # N = 1..4; threshold 2: rewards at N=2 (2nd) and N=4 (4th)
        item = begin("new", "loyal", 1000, package_id=SHARED, claimed_telegram_id=7004)
        run(lambda db: bot_router.purchase_package("loyal", schemas.BotPurchasePackageRequest(
            package_id=SHARED, paid_amount=1000, payment_method="card", approval_uuid=item["approval_uuid"],
            execution_token=item["execution_token"], list_price=1000), db=db, owner_admin_id=None, principal=internal))
        bought.append(item["approval_uuid"])
    with Session() as db:
        loyal = db.query(models.User).filter_by(username="loyal").one()
        before = (loyal.purchase_count, loyal.loyalty_rewards_given, int(loyal.balance))
    check(L + "four paid purchases: two loyalty rewards (100 each)", before, (4, 2, 200))
    done = void(preview(bought[1])["preview_token"], "loyal")         # the purchase that earned the first reward
    with Session() as db:
        loyal = db.query(models.User).filter_by(username="loyal").one()
        rewards = {r.causation_approval_uuid: (r.state, r.void_reason, r.void_operation_id) for r in db.execute(
            select(rv.loyalty_reward_events).where(rv.loyalty_reward_events.c.user_id == loyal.id))}
        own_effect = db.execute(select(rv.receipt_approval_effects.c.reversal_state).where(
            rv.receipt_approval_effects.c.approval_uuid == bought[1],
            rv.receipt_approval_effects.c.effect_key.like("credit:loyalty:%"))).scalar()
        other_effect = db.execute(select(rv.receipt_approval_effects.c.reversal_state,
                                         rv.receipt_approval_effects.c.void_operation_id).where(
            rv.receipt_approval_effects.c.approval_uuid == bought[3],
            rv.receipt_approval_effects.c.effect_key.like("credit:loyalty:%"))).one()
        after = (loyal.purchase_count, loyal.loyalty_rewards_given, int(loyal.balance))
    check(L + "void of the 2nd purchase: N=3, so the reward at N=4 (another approval's) is the one not backed",
          (done["state"], rewards[bought[3]][:2], rewards[bought[3]][2] == done["operation_id"], after),
          ("completed", ("voided", "loyalty_reconciliation"), True, (3, 1, 100)))
    check(L + "...the voided purchase's own reward stays (still backed); its effect not_applicable",
          (rewards[bought[1]][0], own_effect), ("active", "not_applicable"))
    check(L + "...the healthy approval stays completed; its effect reversed by this void (audit)",
          (approval(bought[3])["state"], tuple(other_effect)), ("completed", ("fully_reversed", done["operation_id"])))
    with Session() as db:
        audit = operator.audit(db, ROOT_ADMIN, bought[1])
    check(L + "...the audit of the void shows the other approval's reversed effect",
          [e["approval_uuid"] for e in audit["other_approvals_effects_reversed"]], [bought[3]])

    # ================================================================ renew: the whole account, others warned
    first = begin("new", "renewer", 1000, package_id=SHARED, claimed_telegram_id=7003)
    run(lambda db: bot_router.purchase_package("renewer", schemas.BotPurchasePackageRequest(
        package_id=SHARED, paid_amount=1000, payment_method="card", approval_uuid=first["approval_uuid"],
        execution_token=first["execution_token"], list_price=1000), db=db, owner_admin_id=None, principal=internal))
    renewal = begin("renew", "renewer", 1000, package_id=SHARED)
    run(lambda db: bot_router.renew_service("renewer", renewal["renew_purchase_id"], schemas.BotRenewRequest(
        add_gb=10, add_days=30, package_id=SHARED, paid_amount=1000, payment_method="card",
        approval_uuid=renewal["approval_uuid"], execution_token=renewal["execution_token"], list_price=1000),
        db=db, owner_admin_id=None, principal=internal))
    shown = preview(renewal["approval_uuid"])
    check(L + "preview renew: the whole account, the other approval on it warned",
          (approval(renewal["approval_uuid"])["state"], shown["resources"]["delete"]["kind"],
           [a["approval_uuid"] for a in shown["other_approvals_on_account"]]),
          ("completed", "user", [first["approval_uuid"]]))

    # ---- an unverified absence: cleanup_required, nothing deleted; retry converges
    ABSENT["mode"] = "unverified"
    done = void(shown["preview_token"], "renewer")
    op_renew = done["operation_id"]
    renew_steps = steps_of(op_renew)
    verify = next(k for k in renew_steps if k.startswith("verify:"))
    check(L + "void renew with an unverified absence: cleanup_required, the verify step blocked 'unverified'",
          (done["state"], approval(renewal["approval_uuid"])["state"], renew_steps[verify], renew_steps["db_delete"][0]),
          ("cleanup_required", "cleanup_required", ("blocked", "unverified"), "pending"))
    check(L + "...the user and its rows are NOT deleted; the sale not yet voided",
          (user_id("renewer") is not None, run(lambda db: db.query(models.LedgerEntry).filter_by(
              approval_uuid=renewal["approval_uuid"], kind="sale_renew").one().voided_at)), (True, None))
    with Session() as db:
        check(L + "the worker cannot go off with a void in cleanup_required",
              code(lambda: (flags.set_flag(db, "execute", False), flags.set_flag(db, "worker", False))[1]),
              (409, "receipt_void_operations_active"))
        flags.set_flag(db, "execute", True)
    ABSENT["mode"] = "absent"
    retried = code(lambda: operator.retry(Session, ROOT_ADMIN, op_renew))
    check(L + "retry after the node answers: completed, the account deleted",
          (retried["state"], approval(renewal["approval_uuid"])["state"], user_id("renewer")), ("completed", "voided", None))
    check(L + "...the other approval (healthy, deleted with the account) stays completed",
          approval(first["approval_uuid"])["state"], "completed")
    with Session() as db:
        reward = db.execute(select(rv.loyalty_reward_events).where(
            rv.loyalty_reward_events.c.causation_approval_uuid == renewal["approval_uuid"])).mappings().one()
        credit_effect = db.execute(select(rv.receipt_approval_effects.c.reversal_state).where(
            rv.receipt_approval_effects.c.approval_uuid == renewal["approval_uuid"],
            rv.receipt_approval_effects.c.effect_key.like("credit:loyalty:%"))).scalar()
    check(L + "...the loyalty reward the renewal earned: voided (receipt_void), its credit taken back",
          (reward["state"], reward["void_reason"], reward["credit_reversal_state"], credit_effect),
          ("voided", "receipt_void", "fully_reversed", "fully_reversed"))
    check(L + "retry of a completed void: 409", code(lambda: operator.retry(Session, ROOT_ADMIN, op_renew)),
          (409, "receipt_void_not_retryable"))

    # ---- forced: an orphan for manual cleanup, then the void finishes
    forced = begin("new", "scam2", 1000, package_id=SHARED, claimed_telegram_id=7102)
    run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
        username="scam2", telegram_id=7102, package_id=SHARED, paid_amount=1000, payment_method="card",
        approval_uuid=forced["approval_uuid"], execution_token=forced["execution_token"], list_price=1000),
        db=db, principal=internal))
    ABSENT["mode"] = "unverified"
    done = void(preview(forced["approval_uuid"])["preview_token"], "scam2")
    ABSENT["mode"] = "absent"
    verify = next(k for k in steps_of(done["operation_id"]) if k.startswith("verify:"))
    check(L + "force needs danger_ack", code(lambda: operator.force_step(
        Session, ROOT_ADMIN, done["operation_id"], step_key=verify, reason="node destroyed", danger_ack=False)),
        (422, "danger_ack_required"))
    check(L + "a step that is not forceable: 409", code(lambda: operator.force_step(
        Session, ROOT_ADMIN, done["operation_id"], step_key="wallet_reversal", reason="x", danger_ack=True)),
        (409, "receipt_void_step_not_forceable"))
    result = code(lambda: operator.force_step(Session, ROOT_ADMIN, done["operation_id"], step_key=verify,
                                              reason="node destroyed", danger_ack=True))
    with Session() as db:
        orphan = db.execute(select(mp.ProvisioningStep.remote_outcome, mp.ProvisioningStep.forced_by_admin_id).where(
            mp.ProvisioningStep.remote_outcome == "abandoned")).all()
        log = operator.status(db, ROOT_ADMIN, done["operation_id"])["forced"]
    check(L + "force verify: abandoned (orphan with the admin), the void finishes, the account deleted",
          (result["state"], steps_of(done["operation_id"])[verify], [tuple(o) for o in orphan], user_id("scam2"),
           [(e["step_key"], e["admin_id"], e["reason"]) for e in log]),
          ("completed", ("abandoned", "abandoned"), [("abandoned", ROOT)], None, [(verify, ROOT, "node destroyed")]))

    # ================================================================ crash / failure of a later step
    crash = begin("new", "scam3", 1000, package_id=SHARED, claimed_telegram_id=7103)
    run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
        username="scam3", telegram_id=7103, package_id=SHARED, paid_amount=1000, payment_method="card",
        approval_uuid=crash["approval_uuid"], execution_token=crash["execution_token"], list_price=1000),
        db=db, principal=internal))
    with patch.object(user_ops, "finalize_user_deletion_after_deprovision", side_effect=RuntimeError("db down")), \
            patch.object(void_steps, "ledger", side_effect=RuntimeError("crash")):
        done = void(preview(crash["approval_uuid"])["preview_token"], "scam3")
    check(L + "the db_delete transaction fails: rolled back, the user and its rows stay, the void still running",
          (done["state"], user_id("scam3") is not None, steps_of(done["operation_id"])["db_delete"][0]),
          ("running", True, "pending"))
    with Session() as db:                     # the backoff is over
        db.execute(rv.wallet_operation_steps.update().where(
            rv.wallet_operation_steps.c.wallet_operation_id == done["operation_id"]).values(next_retry_at=None))
        db.execute(mp.ProvisioningOperation.__table__.update().values(next_retry_at=None))
        db.commit()
    with patch.object(void_steps, "ledger", side_effect=RuntimeError("crash")):
        for _ in range(3):
            operator.recover_due(Session)
            durable.recover_due(Session)
    states = steps_of(done["operation_id"])
    with Session() as db:
        error = next(s["error_sanitized"] for s in void_engine._steps(db, done["operation_id"])
                     if s["step_key"] == "ledger_reversal")
    check(L + "...the worker resumes: deleted, then the crashing ledger step is retried with a sanitized error",
          (user_id("scam3"), states["db_delete"][0], states["wallet_reversal"][0], states["ledger_reversal"][0], error),
          (None, "done", "done", "failed", "RuntimeError"))
    with Session() as db:
        db.execute(rv.wallet_operation_steps.update().where(
            rv.wallet_operation_steps.c.wallet_operation_id == done["operation_id"]).values(next_retry_at=None))
        db.commit()
    operator.recover_due(Session)
    check(L + "...after the crash: resumed from that step, completed; nothing reversed twice",
          (steps_of(done["operation_id"])["finalize"][0], approval(crash["approval_uuid"])["state"],
           run(lambda db: db.query(models.LedgerEntry).filter_by(approval_uuid=crash["approval_uuid"],
                                                                 kind="receipt_void").count())), ("done", "voided", 1))

    # ================================================================ concurrent execute: one operation
    race = begin("new", "scam4", 1000, package_id=SHARED, claimed_telegram_id=7104)
    run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
        username="scam4", telegram_id=7104, package_id=SHARED, paid_amount=1000, payment_method="card",
        approval_uuid=race["approval_uuid"], execution_token=race["execution_token"], list_price=1000),
        db=db, principal=internal))
    token = preview(race["approval_uuid"])["preview_token"]
    results = []
    barrier = threading.Barrier(2)

    def contender():
        barrier.wait()
        try:
            results.append(void(token, "scam4"))
        except Exception as exc:  # noqa: BLE001 - recorded, checked below
            results.append(repr(exc)[:200])

    threads = [threading.Thread(target=contender) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with Session() as db:
        operations = db.execute(select(func.count()).select_from(rv.wallet_operations).where(
            rv.wallet_operations.c.business_key == race["approval_uuid"])).scalar_one()
        pairs = db.query(models.LedgerEntry).filter_by(approval_uuid=race["approval_uuid"], kind="receipt_void").count()
    ids = {r["operation_id"] for r in results if isinstance(r, dict)}
    check(L + "two concurrent executes: both answered with the same operation, one reversal",
          (operations, len(ids), pairs, [isinstance(r, dict) for r in results]), (1, 1, 1, [True, True]))
    with Session() as db:
        check(L + "the reconciliation finds nothing wrong at the end", wallet_reconcile.check(db)["problems"], [])
    patches.close()


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="receipt_void_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    def on(label, engine):
        with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}):
            scenario(label, engine)

    on("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        on("mariadb", maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
