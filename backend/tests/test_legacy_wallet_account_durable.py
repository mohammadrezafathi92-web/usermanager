"""A durable operation on a User created before P5 (no wallet account in the
'normal' generation - 814 of 941 on production), on SQLite AND real MariaDB
(CI). Fake runner; no network.

Found 2026-10-18 (receipt #1022, user 5273): T1 read the target's account
with scalar_one() and every purchase/renewal of such a customer failed with
NoResultFound - nothing committed, the approval failed. Now the identity T1
leases is resolved in its own commit before T1
(wallet_accounts.resolve_for_operation, called by provisioning_durable.run).

  approval      a shadow receipt approval of an existing pre-P5 customer
                completes with its manifest; its replay changes nothing
  money         a wallet sale of a pre-P5 customer with a positive balance
                takes it from User.balance; accounts get NO lot
  shared tg     two pre-P5 Users with one Telegram ID: one identity in the
                tenant, one account each; a new customer with that ID too
  every path    renew_purchase, renew_user, add_connection and the three
                deletions of pre-P5 Users; a User that already has an
                account keeps exactly that one
  T1 failure    no operation, sale, payment hold or effect; the approval is
                not mutating; only the account (no lot) remains
  cutover       'fencing' refuses before anything is written; a T1 started
                while the cutover holds the phase waits, then runs on the
                account the backfill made: one account per User, exactly one
                baseline lot per positive balance, reconciliation clean;
                in 'enforced' a User without account is refused, never given
                an account without its baseline
"""
import contextlib
import datetime as dt
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
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-legacy-wallet-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, provisioning_contracts as contracts, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_preparation as preparation
from app.services import provisioning_readiness, provisioning_requests as requests, provisioning_schema
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_schema, remote_action as action, user_ops, wallet_accounts, wallet_cutover
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


def detail(exc):
    return exc.detail if isinstance(exc.detail, str) else (exc.detail.get("error"), exc.detail.get("state"))


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Session)["ready"],
                                provisioning_schema.bootstrap(engine, Session)["ready"]), (True, True))
    host = HostIdentity(("a7" if label == "sqlite" else "b7") * 32, str(uuid.uuid4()))
    durable.read_identity = lambda: host
    ids = {}
    with Session() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.owner_state, row.owner_host_id, row.owner_boot_id = "active", host.host_id, host.boot_id
        row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = dt.datetime.utcnow()
        row.ownership_epoch, row.gate_mode = 1, "enforced"
        row.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.add(models.PanelSettings(id=1))
        root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
        db.add(root)
        db.flush()
        node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.33.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.73.0.0/24")
        db.add(node)
        db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
            verified_at=dt.datetime.utcnow()))
        package = models.Package(name="Shared", quota_gb=10, duration_days=30, price=1000)
        db.add(package)
        db.flush()
        db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
        # Pre-P5 customers: a bare User row, as production has 814 of them.
        legacy = dict(old0=(950, 0), oldpos=(950, 5000), oldfail=(951, 0), oldren=(952, 0), oldrenu=(953, 0),
                      oldadd=(954, 0), olddc=(955, 0), olddp=(956, 0), olddu=(957, 0), oldfence=(958, 0),
                      oldrace=(959, 300))
        for name, (telegram_id, balance) in legacy.items():
            user = models.User(username=name, telegram_id=telegram_id, balance=balance,
                               status=models.UserStatus.active, total_quota_bytes=10 * 1024 ** 3,
                               expire_at=dt.datetime.utcnow() + dt.timedelta(days=10))
            db.add(user)
            db.flush()
            ids[name] = user.id
        ids["fresh"] = wallet_accounts.create_user_with_wallet(db, username="fresh", telegram_id=960).id
        db.flush()
        for name in ("oldren", "olddp"):
            purchase = models.Purchase(user_id=ids[name], package_id=package.id, status=models.UserStatus.active,
                                       quota_bytes=10 * 1024 ** 3, expire_at=dt.datetime.utcnow() + dt.timedelta(days=5))
            db.add(purchase)
            db.flush()
            ids[name + "_purchase"] = purchase.id
        for name, purchase in (("olddc", None), ("olddp", ids["olddp_purchase"]), ("olddu", None)):
            connection = models.Connection(user_id=ids[name], purchase_id=purchase, node_id=node.id,
                type=models.ConnectionType.wireguard, enabled=True, wg_peer_name=f"peer-{name}-{label}",
                wg_public_key=f"PUB-{name}", wg_client_address="10.73.0.9/32")
            db.add(connection)
            db.flush()
            ids[name + "_connection"] = connection.id
        db.commit()
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", loyalty_timing_mode="legacy_activation", key_identity_ready=True,
            key_identity_failure=None))
        for kind in mp.OPERATION_TYPES:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        PACKAGE, NODE, ROOT = package.id, node.id, root.id
    principal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)
    sequence = iter(range(1, 1000))

    def accounts(name):
        """(live account ids, all account ids, identity ids) of a User."""
        with Session() as db:
            rows = db.execute(select(rv.wallet_accounts).where(
                rv.wallet_accounts.c.user_id_snapshot == ids[name])).mappings().all()
        return ([r["id"] for r in rows if r["user_id"] is not None], [r["id"] for r in rows],
                sorted({r["customer_identity_id"] for r in rows}))

    def lots():
        with Session() as db:
            return db.execute(select(func.count()).select_from(rv.wallet_lots)).scalar_one()

    def run(call, runner=ok_runner):
        with Session() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")), \
                patch.object(execute.remote_runner, "run_action", side_effect=runner):
            try:
                call(db)
                return "ok"
            except HTTPException as exc:
                return detail(exc)

    def durable_run(build):
        def call(db):
            request = build(db)
            factory = requests.session_factory_for(db)
            db.rollback()
            durable.run(factory, request, actor_kind="admin", actor_id=ROOT)
        return run(call)

    def register(username, telegram_id):
        item = ri.ApprovalIntent(pending_source_instance_id=f"legacy-{label}", pending_local_id=next(sequence),
                                 kind="new", target_username=username, amount=1000, package_id=PACKAGE,
                                 list_price=1000, claimed_telegram_id=telegram_id)
        with Session() as db:
            result = registration.register(db, principal, item, approval_mode="auto")
            db.commit()
            return result.approval_uuid, result.execution_token

    def buy(username, approval=None, key=None, method="card"):
        extra = dict(approval_uuid=approval[0], execution_token=approval[1], list_price=1000) if approval else {}
        wallet = dict(charge_wallet=True) if method == "wallet" else {}
        return run(lambda db: bot_router.purchase_package(username, schemas.BotPurchasePackageRequest(
            package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
            paid_amount=1000, payment_method=method, **wallet, **extra), db=db, owner_admin_id=None,
            principal=principal, **({"x_idempotency_key": key} if key else {})))

    def approval_state(approval_uuid):
        with Session() as db:
            state = db.execute(select(rv.receipt_approvals.c.state).where(
                rv.receipt_approvals.c.approval_uuid == approval_uuid)).scalar_one()
            effects = db.execute(select(func.count()).select_from(rv.receipt_approval_effects).where(
                rv.receipt_approval_effects.c.approval_uuid == approval_uuid)).scalar_one()
            expected = db.execute(select(func.count()).select_from(rv.receipt_approval_expected_effects).where(
                rv.receipt_approval_expected_effects.c.approval_uuid == approval_uuid)).scalar_one()
            operations = db.execute(select(func.count()).select_from(mp.ProvisioningOperation.__table__).where(
                mp.ProvisioningOperation.approval_uuid == approval_uuid)).scalar_one()
        return state, effects, expected, operations

    def totals():
        with Session() as db:
            return (db.query(mp.ProvisioningOperation).count(), db.query(models.Purchase).count(),
                    db.query(models.LedgerEntry).count(), db.query(mp.PaymentReservation).count())

    # ------------------------------------------------------------ the #1022 shape: shadow approval, existing customer
    check(L + "a pre-P5 customer starts with no account", accounts("old0"), ([], [], []))
    approval = register("old0", 950)
    outcome = buy("old0", approval)
    state, effects, expected, operations = approval_state(approval[0])
    live, every, identities = accounts("old0")
    check(L + "shadow approval of a pre-P5 customer's purchase: completed with its manifest, one operation",
          (outcome, state, effects == expected > 0, operations), ("ok", "completed", True, 1))
    check(L + "...the customer got exactly one account (identity + account), and no lot",
          (len(live), len(every), len(identities), lots()), (1, 1, 1, 0))
    before = totals()
    replay = buy("old0", approval)
    check(L + "the same approval again: answered from its operation, nothing new, still one account",
          (replay, approval_state(approval[0]), totals(), accounts("old0")),
          ("ok", (state, effects, expected, 1), before, (live, every, identities)))
    key = str(uuid.uuid4())
    first, again = buy("old0", key=key), None
    after_first = totals()
    again = buy("old0", key=key)
    check(L + "an idempotency-key replay of a card sale: one operation, one sale",
          (first, again, totals()), ("ok", "ok", after_first))

    # ------------------------------------------------------------ money: wallet sale of a positive balance, shared tg
    outcome = buy("oldpos", method="wallet")
    with Session() as db:
        balance = db.get(models.User, ids["oldpos"]).balance
    pos_live, pos_every, pos_identities = accounts("oldpos")
    check(L + "wallet sale of a pre-P5 customer: paid from User.balance (5000 -> 4000), no lot",
          (outcome, balance, lots()), ("ok", 4000, 0))
    check(L + "two Users with one Telegram ID: one identity in the tenant, an account each",
          (pos_identities == identities, len(pos_live), pos_live != live), (True, 1, True))
    with Session() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")), \
            patch.object(execute.remote_runner, "run_action", side_effect=ok_runner):
        try:
            bot_router.create_user(schemas.BotCreateUserRequest(username="sibling", telegram_id=950,
                package_id=PACKAGE, connections=[schemas.BotCreateConnectionSpec(node_id=NODE, protocol="wireguard")],
                paid_amount=1000, payment_method="card"), db=db, principal=principal,
                x_idempotency_key=str(uuid.uuid4()))
            created = "ok"
        except HTTPException as exc:
            created = detail(exc)
    with Session() as db:
        ids["sibling"] = db.query(models.User).filter_by(username="sibling").one().id
    sib_live, _, sib_identities = accounts("sibling")
    check(L + "a new customer with the same Telegram ID: created, the same identity, its own account",
          (created, sib_identities == identities, len(sib_live), sib_live not in (live, pos_live)), ("ok", True, 1, True))

    # ------------------------------------------------------------ every other durable path of a pre-P5 User
    with Session() as db:
        db_package = db.get(models.Package, PACKAGE)
    outcome = durable_run(lambda db: requests.renewal(db, db.get(models.User, ids["oldren"]),
        purchase=db.get(models.Purchase, ids["oldren_purchase"]), add_days=30, key=str(uuid.uuid4())))
    check(L + "renew_purchase of a pre-P5 customer: completed, one account, no lot",
          (outcome, len(accounts("oldren")[0]), lots()), ("ok", 1, 0))
    outcome = durable_run(lambda db: requests.renewal(db, db.get(models.User, ids["oldrenu"]), purchase=None,
                                                      add_days=30, key=str(uuid.uuid4())))
    check(L + "renew_user of a pre-P5 customer: completed, one account", (outcome, len(accounts("oldrenu")[0])),
          ("ok", 1))
    outcome = durable_run(lambda db: requests.add_connection(db, db.get(models.User, ids["oldadd"]),
                                                             db.get(models.Node, NODE), "wireguard", key=str(uuid.uuid4())))
    check(L + "add_connection of a pre-P5 customer: completed, one account", (outcome, len(accounts("oldadd")[0])),
          ("ok", 1))
    for kind, name, resource in (("connection", "olddc", ids["olddc_connection"]),
                                 ("purchase", "olddp", ids["olddp_purchase"]), ("user", "olddu", ids["olddu"])):
        def delete(db, kind=kind, resource=resource):
            requests.run_delete(db, kind, resource, None, key=str(uuid.uuid4()), actor_kind="admin", actor_id=ROOT)
        outcome = run(delete)
        with Session() as db:
            gone = {"connection": models.Connection, "purchase": models.Purchase, "user": models.User}[kind]
            remaining = db.get(gone, resource)
        live_d, every_d, _ = accounts(name)
        check(L + f"delete_{kind} of a pre-P5 customer: completed, the resource removed",
              (outcome, remaining is None), ("ok", True))
        check(L + f"...delete_{kind}: one account ({'tombstoned with its User' if kind == 'user' else 'live'})",
              (len(live_d), len(every_d)), (0 if kind == "user" else 1, 1))
    fresh_before = accounts("fresh")
    outcome = buy("fresh")
    check(L + "a User that already had an account keeps exactly that one",
          (outcome, accounts("fresh")), ("ok", fresh_before))

    # ------------------------------------------------------------ T1 failure: nothing financial behind
    approval = register("oldfail", 951)
    before = totals()
    with patch.object(preparation.contracts, "require_ready", side_effect=HTTPException(409, "node_contract_not_ready")):
        outcome = buy("oldfail", approval)
    state, effects, _, operations = approval_state(approval[0])
    check(L + "T1 fails after the account was resolved: refused, no operation/purchase/sale/hold",
          (outcome, totals()), ("node_contract_not_ready", before))
    check(L + "...the approval is not mutating and has no effect; only the account (no lot) remains",
          (state != "mutating", effects, operations, len(accounts("oldfail")[0]), lots()), (True, 0, 0, 1, 0))
    with Session() as db:
        db.execute(rv.receipt_approvals.update().where(rv.receipt_approvals.c.approval_uuid == approval[0]).values(
            state="cancelled", cancellation_reason="test_done"))
        db.commit()

    # ------------------------------------------------------------ the cutover
    with Session() as db:
        db.execute(rv.wallet_runtime_state.update().values(phase="fencing"))
        db.commit()
    before = totals()
    outcome = durable_run(lambda db: requests.purchase(db, db.get(models.User, ids["oldfence"]), db.get(
        models.Package, PACKAGE), slots=requests.package_slots(db, db.get(models.Package, PACKAGE)),
        key=str(uuid.uuid4())))
    check(L + "'fencing': refused before anything is written - no account, no operation",
          (outcome, accounts("oldfence"), totals()), ("wallet_writer_unavailable", ([], [], []), before))
    with Session() as db:
        db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
        db.commit()

    backfilling, finish = threading.Event(), threading.Event()
    real_backfill = wallet_cutover._backfill

    def paused_backfill(db):
        result = real_backfill(db)
        backfilling.set()
        finish.wait(30)
        return result

    def threaded(target):
        out = {}
        thread = threading.Thread(target=lambda: out.update(result=target()))
        thread.start()
        return thread, out

    def cutover():
        with Session() as db:
            try:
                return wallet_cutover.cutover(db)["phase"]
            except HTTPException as exc:
                return detail(exc)

    with patch.object(wallet_cutover, "_backfill", side_effect=paused_backfill):
        cut_thread, cut = threaded(cutover)
        backfilling.wait(30)                       # the cutover holds the phase row, the backfill is done
        buy_thread, bought = threaded(lambda: buy("oldrace"))
        time.sleep(1.5)
        waited = buy_thread.is_alive()
        finish.set()
        cut_thread.join(60)
        buy_thread.join(90)
    race_live, race_every, _ = accounts("oldrace")
    check(L + "a T1 started while the cutover holds the phase waits, then completes on the backfill's account",
          (cut.get("result"), waited, bought.get("result"), len(race_live), len(race_every)),
          ("enforced", True, "ok", 1, 1))
    with Session() as db:
        users = db.query(models.User.id, models.User.balance).all()
        live_counts = dict(db.execute(select(rv.wallet_accounts.c.user_id, func.count()).where(
            rv.wallet_accounts.c.user_id.isnot(None)).group_by(rv.wallet_accounts.c.user_id)).all())
        baseline = dict(db.execute(select(rv.wallet_lots.c.wallet_account_id, func.count()).where(
            rv.wallet_lots.c.source_kind == "legacy_baseline").group_by(rv.wallet_lots.c.wallet_account_id)).all())
        amounts = dict(db.execute(select(rv.wallet_accounts.c.user_id, rv.wallet_lots.c.original_amount).join(
            rv.wallet_lots, rv.wallet_lots.c.wallet_account_id == rv.wallet_accounts.c.id).where(
            rv.wallet_lots.c.source_kind == "legacy_baseline")).all())
        mismatches = wallet_cutover._reconcile(db)
    positive = {uid: int(balance) for uid, balance in users if (balance or 0) > 0}
    check(L + "after the cutover: exactly one live account per User",
          (set(live_counts) == {uid for uid, _ in users}, set(live_counts.values())), (True, {1}))
    check(L + "...exactly one baseline lot per positive balance, of that balance (oldpos 4000, oldrace 300)",
          (amounts, set(baseline.values()), mismatches),
          (positive, {1}, []))
    check(L + "...the two pre-P5 positive balances are among them",
          (amounts.get(ids["oldpos"]), amounts.get(ids["oldrace"])), (4000, 300))

    with Session() as db:
        ghost = models.User(username="ghost", telegram_id=970, status=models.UserStatus.active)
        db.add(ghost)              # an impossible User after the cutover: no account
        db.commit()
        ids["ghost"] = ghost.id
    before, lots_before = totals(), lots()
    outcome = buy("ghost")
    check(L + "'enforced', a User without account: refused, no account synthesized without its baseline",
          (outcome, accounts("ghost"), totals(), lots()), ("wallet_account_not_ready", ([], [], []), before,
                                                           lots_before))


with contextlib.ExitStack() as cleanup, patch.object(provisioning_readiness, "p6_ready", return_value=(True, [])):
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="legacy_wallet_"))
    sqlite = create_engine(f"sqlite:///{folder}/t.db", connect_args={"check_same_thread": False, "timeout": 30})
    cleanup.callback(sqlite.dispose)

    @event.listens_for(sqlite, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    def on(label, engine):
        with patch.dict(os.environ, {"DATABASE_URL": engine.url.render_as_string(hide_password=False)}), \
                patch("app.database.SessionLocal", sessionmaker(bind=engine, autoflush=False)):
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
