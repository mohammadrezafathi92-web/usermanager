"""The durable receipt-approval path on SQLite AND real MariaDB (CI): the
owner-approved lock order (wallet/receipt runtime -> approval row -> leases
-> targets) under real row locks. Fake runner; no network beyond the
scratch database.

  * required purchase by a reseller's customer with a discount: T1 binds and
    reserves, T_final records every effect, captures, redeems - completed;
  * shadow brand-new customer with a bundled package: completed;
  * the execution key revoked after T1: cleanup_required + one recovery;
    superadmin continue -> completed through the worker.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-approval-engines-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, provisioning_approval_final as approval_final
from app.services import provisioning_contracts as contracts, provisioning_durable as durable
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


def ok_runner(dto):
    if dto.action_type is action.ActionType.WG_ENSURE_ABSENT:
        return action.RemoteActionResult(dto.action_id, action.Outcome.ABSENT_VERIFIED, write_attempted=True,
                                         remote_outcome="verified_absent")
    return action.RemoteActionResult(dto.action_id, action.Outcome.SUCCEEDED, write_attempted=True)


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Session)["ready"],
                                provisioning_schema.bootstrap(engine, Session)["ready"]), (True, True))
    host_identity = HostIdentity(("ab" if label == "sqlite" else "cd") * 32, str(uuid.uuid4()))
    durable.read_identity = lambda: host_identity
    with Session() as db:
        host = db.get(mp.ProvisioningRuntimeState, 1)
        host.owner_state, host.owner_host_id, host.owner_boot_id = "active", host_identity.host_id, host_identity.boot_id
        host.owner_claimed_at = host.owner_heartbeat_at = host.lock_verified_at = dt.datetime.utcnow()
        host.ownership_epoch, host.gate_mode = 1, "enforced"
        host.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.add(models.PanelSettings(id=1))
        # This execution fixture permits auto approvals explicitly; policy denial
        # is covered separately by the central auto-approval tests.
        db.add(models.BotSettings(id=1, auto_approve_enabled=True,
                                  auto_approve_ignore_hours=True, auto_approve_returning_only=False))
        root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
        reseller = models.AdminUser(username="res", hashed_password="x", balance=5000)
        key = models.ApiKey(key=f"remote-{label}", label="remote", key_type=bot_auth.KeyType.REMOTE_SHARED_BOT)
        db.add_all([root, reseller, key])
        db.flush()
        node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.16.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.61.0.0/24")
        db.add(node)
        db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
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
        db.add(models.DiscountCode(code="OFF10", kind="percent", value=10, enabled=True, owner_admin_id=reseller.id))
        wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=902, owner_admin_id=reseller.id)
        wallet_accounts.create_user_with_wallet(db, username="plain", telegram_id=903)
        db.commit()
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1,
            key_identity_ready=True, key_identity_failure=None))
        for kind in ("create_user", "purchase", "renew_purchase"):
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        SHARED, OWNED, NODE, RESELLER, KEY, ROOT = shared.id, owned.id, node.id, reseller.id, key.id, root.id
        remote = bot_auth.BotPrincipal.from_api_key(db.get(models.ApiKey, KEY))
    internal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
    sequence = iter(range(1, 100))

    def register(username, package, amount, principal, mode, **extra):
        item = ri.ApprovalIntent(pending_source_instance_id=f"engines-{label}", pending_local_id=next(sequence),
                                 kind="new", target_username=username, amount=amount, package_id=package,
                                 list_price=extra.pop("list_price", amount), **extra)
        with Session() as db:
            result = registration.register(db, principal, item, approval_mode="auto")
            if mode == "required":
                db.execute(rv.receipt_approvals.update().where(
                    rv.receipt_approvals.c.approval_uuid == result.approval_uuid).values(
                        registered_under_mode="required", completeness_generation=1))
            db.commit()
            return result.approval_uuid, result.execution_token

    def run(call, runner=ok_runner):
        with Session() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")), \
                patch.object(execute.remote_runner, "run_action", side_effect=runner):
            try:
                call(db)
                return "ok"
            except HTTPException as exc:
                return exc.detail if isinstance(exc.detail, str) else (exc.detail.get("error"), exc.detail.get("state"))

    def state(approval_uuid):
        with Session() as db:
            row = db.execute(select(rv.receipt_approvals.c.state).where(
                rv.receipt_approvals.c.approval_uuid == approval_uuid)).scalar_one()
            effects = len(db.execute(select(rv.receipt_approval_effects.c.id).where(
                rv.receipt_approval_effects.c.approval_uuid == approval_uuid)).all())
            expected = len(db.execute(select(rv.receipt_approval_expected_effects.c.id).where(
                rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid)).all())
            operation = db.execute(select(mp.ProvisioningOperation.state).where(
                mp.ProvisioningOperation.approval_uuid == approval_uuid)).scalar_one_or_none()
            return row, effects, expected, operation

    # required purchase, reseller credit + discount
    u1, t1 = register("cust", OWNED, 450, internal, "required", discount_code="off10", list_price=500,
                      claimed_telegram_id=902)
    outcome = run(lambda db: bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
        package_id=OWNED, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
        paid_amount=450, payment_method="card", discount_code="off10", approval_uuid=u1, execution_token=t1,
        list_price=500), db=db, owner_admin_id=None, principal=internal))
    approval_state, effects, expected, operation = state(u1)
    with Session() as db:
        spent = 5000 - db.get(models.AdminUser, RESELLER).balance
        used = db.query(models.DiscountCode).filter_by(code="OFF10").one().used_count
    check(L + "required purchase: completed with every effect, credit captured once, discount used once",
          (outcome, operation, approval_state, effects == expected, spent, used),
          ("ok", "completed", "completed", True, 300, 1))

    # shadow new customer
    with Session() as db:
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", loyalty_timing_mode="legacy_activation"))
        db.commit()
    u2, t2 = register("fresh", SHARED, 1000, internal, "shadow", claimed_telegram_id=904)
    outcome = run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
        username="fresh", telegram_id=904, package_id=SHARED, connections=[schemas.BotCreateConnectionSpec(
            node_id=NODE, protocol="wireguard")], paid_amount=1000, payment_method="card", approval_uuid=u2,
        execution_token=t2, list_price=1000), db=db, principal=internal))
    approval_state, effects, expected, operation = state(u2)
    check(L + "shadow new customer: completed, the approval completed with its manifest",
          (outcome, operation, approval_state, effects == expected), ("ok", "completed", "completed", True))

    # key revoked after T1, then superadmin continue
    def revoke_then_succeed(dto):
        with Session() as db:
            db.get(models.ApiKey, KEY).enabled = False
            db.commit()
        return ok_runner(dto)

    u3, t3 = register("plain", SHARED, 1000, remote, "shadow", claimed_telegram_id=903)
    outcome = run(lambda db: bot_router.purchase_package("plain", schemas.BotPurchasePackageRequest(
        package_id=SHARED, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
        paid_amount=1000, payment_method="card", approval_uuid=u3, execution_token=t3, list_price=1000),
        db=db, owner_admin_id=None, principal=remote), runner=revoke_then_succeed)
    check(L + "key revoked after T1: cleanup_required, approval mutating",
          (outcome, state(u3)[3], state(u3)[0]), (("operation_in_progress", "cleanup_required"),
                                                  "cleanup_required", "mutating"))
    approval_final.decide(Session, u3, "continue", ROOT, "checked")
    with patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")), \
            patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
        for _ in range(4):
            durable.recover_due(Session)
    approval_state, effects, expected, operation = state(u3)
    check(L + "superadmin continue: completed through the worker with every effect",
          (operation, approval_state, effects == expected), ("completed", "completed", True))


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="approval_engines_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

    def on(label, engine):
        # The runner's child reads the configured database (DATABASE_URL);
        # it must be the one this scenario writes.
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
