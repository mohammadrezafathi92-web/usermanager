"""Shared setup of the receipt-void end-to-end tests (not a test itself).

build(label, engine) seeds one panel - superadmin, a reseller, a WireGuard
node with a ready contract, two packages, two payment cards (global pool,
event_logged), a discount code, loyalty threshold 2 / 100 per reward,
referral rewards 3000 / 1000 - runs the wallet cutover and the joint
activation, switches the three receipt-void flags on, and returns an Env
with the helpers every scenario uses. The fake runner never touches a
network; ABSENT["mode"] makes ensure_absent answer 'unverified'.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, loyalty_activation, payment_card_events, product_decisions, provisioning_contracts as contracts
from app.services import provisioning_durable as durable, provisioning_parent_execute as execute
from app.services import provisioning_readiness, provisioning_schema, quota_manager
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_engine as void_engine, receipt_void_flags as flags, receipt_void_schema
from app.services import receipt_void_scope as scope, remote_action as action, user_ops, wallet_accounts
from app.services import wallet_cutover
from app.services.provisioning_host import HostIdentity

ABSENT = {"mode": "absent", "unverified_peers": set(), "idempotent_peers": set()}
CALLS = []                                        # (action_type, wg_peer_name) of every ensure_absent
SECRET_KEYS = ("wg_private_key", "wg_preshared_key", "xr_uuid", "ppp_password", "password", "private_key", "psk",
               "execution_token", "subscription_token")


def runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        peer = (dto.identity or {}).get("wg_peer_name")
        CALLS.append(("absent", peer))
        if peer in ABSENT["idempotent_peers"]:
            return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                             remote_outcome="delete_idempotently_absent")
        if ABSENT["mode"] == "unverified" or peer in ABSENT["unverified_peers"]:
            return action.RemoteActionResult(dto.action_id, action.Outcome.CONFLICT, write_attempted=False,
                                             remote_outcome="unverified")
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    if (dto.params or {}).get("recovery_read"):
        return action.RemoteActionResult(dto.action_id, action.Outcome.ALREADY_PRESENT_VERIFIED, write_attempted=False)
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def fake_set_enabled(db, connection, enabled):
    """quota_manager._set_connection_enabled without a node: records it."""
    connection.enabled = enabled


def code(call):
    try:
        return call()
    except HTTPException as exc:
        detail = exc.detail
        return exc.status_code, detail if isinstance(detail, str) else detail.get("error")


def leaks(document) -> list:
    text = json.dumps(document, default=str)
    return [k for k in SECRET_KEYS if f'"{k}"' in text]


def sqlite_engine(path):
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=20000")
        cursor.close()
    return engine


def build(label, engine, stack: contextlib.ExitStack, activate: bool = True):
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    assert receipt_void_schema.bootstrap(engine, Session)["ready"]
    assert provisioning_schema.bootstrap(engine, Session)["ready"]
    host_identity = HostIdentity(("f1" if engine.dialect.name == "sqlite" else "f2") * 32, str(uuid.uuid4()))
    durable.read_identity = lambda: host_identity
    with Session() as db:
        host = db.get(mp.ProvisioningRuntimeState, 1)
        host.owner_state, host.owner_host_id, host.owner_boot_id = "active", host_identity.host_id, host_identity.boot_id
        host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
        host.ownership_epoch, host.gate_mode = 1, "enforced"
        host.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.add(models.PanelSettings(id=1, loyalty_purchase_threshold=0, loyalty_reward_credit=0,
                                    referral_referrer_reward_credit=3000, referral_new_user_reward_credit=1000))
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
        cards = [models.PaymentCard(card_number=f"60379900000012{n}4", card_holder="Owner", owner_admin_id=None)
                 for n in (3, 5)]
        db.add_all(cards)
        db.add(models.DiscountCode(code="OFF10", kind="percent", value=10, enabled=True, owner_admin_id=reseller.id))
        db.flush()
        payment_card_events.capture_baseline(db, None)
        for name, tg, owner in (("victim", 7001, None), ("resc", 7002, reseller.id), ("renewer", 7003, None),
                                ("loyal", 7004, None), ("referrer", 7005, None)):
            user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg, owner_admin_id=owner)
            if name == "referrer":
                user.referral_code = "REFX"
        db.add(models.LedgerEntry(kind="sale_new", amount=50, username_snapshot="legacy"))
        for kind in mp.OPERATION_TYPES:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", key_identity_ready=True, key_identity_failure=None))
        db.commit()
        ids = SimpleNamespace(shared=shared.id, owned=owned.id, node=node.id, root=root.id, reseller=reseller.id,
                              other=other.id, cards=[c.id for c in cards])
        legacy_entry = db.query(models.LedgerEntry).filter_by(username_snapshot="legacy").one().id
    ready = patch.object(provisioning_readiness, "p6_ready", return_value=(True, []))
    with Session() as db, ready:
        assert wallet_cutover.cutover(db)["phase"] == "enforced"
    with Session() as db:
        db.query(models.PanelSettings).update({"loyalty_purchase_threshold": 2, "loyalty_reward_credit": 100})
        db.commit()
    if activate:
        with Session() as db:
            # the owner's confirmation of the Product decisions the activation needs (addendum)
            product_decisions.confirm(db, ids.root, version=product_decisions.VERSION,
                                      content_hash=product_decisions.CONTENT_HASH)
        with Session() as db, ready:
            assert loyalty_activation.activate(db, ids.root)["activated"]
        with Session() as db:
            for name in ("preview", "worker", "execute"):
                flags.set_flag(db, name, True)
    stack.enter_context(patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")))
    stack.enter_context(patch.object(execute.remote_runner, "run_action", side_effect=runner))
    stack.enter_context(patch.object(quota_manager, "_set_connection_enabled", side_effect=fake_set_enabled))
    internal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
    sequence = iter(range(1, 10_000))

    def admin(ident):
        with Session() as db:
            row = db.get(models.AdminUser, ident)
            db.expunge(row)
            return row

    env = SimpleNamespace(label=label, engine=engine, Session=Session, ids=ids, internal=internal,
                          legacy_entry=legacy_entry, root=admin(ids.root), other=admin(ids.other))

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
            return code(lambda: scope.preview(db, who or env.root, approval_uuid=approval_uuid,
                                              ledger_entry_id=ledger_entry_id))

    def void(token, username, reason="fake receipt", ack=True):
        return code(lambda: void_engine.execute(Session, env.root, preview_token=token, typed_username=username,
                                                reason=reason, danger_ack=ack, ip="203.0.113.7"))

    def steps_of(operation_id):
        with Session() as db:
            return {s["step_key"]: (s["state"], s["remote_outcome"]) for s in void_engine._steps(db, operation_id)}

    def user_id(name):
        with Session() as db:
            row = db.query(models.User).filter_by(username=name).one_or_none()
            return row.id if row else None

    def new_user(username, telegram_id, amount=1000, **extra):
        item = begin("new", username, amount, package_id=ids.shared, claimed_telegram_id=telegram_id, **extra)
        run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
            username=username, telegram_id=telegram_id, package_id=ids.shared, paid_amount=amount,
            payment_method="card", approval_uuid=item["approval_uuid"], execution_token=item["execution_token"],
            list_price=amount, **{k: v for k, v in extra.items() if k in ("payment_card_id", "referral_code")}),
            db=db, principal=internal))
        return item

    def purchase(username, telegram_id, amount=1000, package=None, **extra):
        package = package or ids.shared
        item = begin("new", username, amount, package_id=package, claimed_telegram_id=telegram_id, **extra)
        run(lambda db: bot_router.purchase_package(username, schemas.BotPurchasePackageRequest(
            package_id=package, paid_amount=amount, payment_method="card", approval_uuid=item["approval_uuid"],
            execution_token=item["execution_token"], list_price=extra.get("list_price", amount),
            **{k: v for k, v in extra.items() if k in ("payment_card_id", "discount_code")}),
            db=db, owner_admin_id=None, principal=internal))
        return item

    def renew(username, amount=1000):
        item = begin("renew", username, amount, package_id=ids.shared)
        run(lambda db: bot_router.renew_service(username, item["renew_purchase_id"], schemas.BotRenewRequest(
            add_gb=10, add_days=30, package_id=ids.shared, paid_amount=amount, payment_method="card",
            approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=amount),
            db=db, owner_admin_id=None, principal=internal))
        return item

    def topup(username, amount):
        item = begin("topup", username, amount)
        run(lambda db: bot_router.add_balance(username, schemas.BotAddBalanceRequest(
            amount=amount, source_kind="receipt_topup", approval_uuid=item["approval_uuid"],
            execution_token=item["execution_token"]), db=db, principal=internal))
        run(lambda db: (registration.finalize(db, internal, item["approval_uuid"]), db.commit()))
        return item

    def wallet_sale(username, amount=1000):
        return run(lambda db: len(bot_router.purchase_package(username, schemas.BotPurchasePackageRequest(
            package_id=ids.shared, paid_amount=amount, payment_method="wallet", charge_wallet=True), db=db,
            owner_admin_id=None, principal=internal, x_idempotency_key=str(uuid.uuid4())).connections))

    env.__dict__.update(begin=begin, run=run, approval=approval, preview=preview, void=void, steps_of=steps_of,
                        user_id=user_id, new_user=new_user, purchase=purchase, renew=renew, topup=topup,
                        wallet_sale=wallet_sale)
    return env


def environment(label, engine, stack, activate: bool = True):
    """DATABASE_URL of the runner's child must be the scenario's database.
    activate=False stops after the wallet cutover (before P9a)."""
    stack.enter_context(patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}))
    return build(label, engine, stack, activate=activate)
