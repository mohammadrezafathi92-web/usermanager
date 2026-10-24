"""The atomic boundaries around the wallet cutover (P9) and the joint
activation (P9a), on SQLite and real MariaDB (CI). Fake runner; no network.

  * HA switched on in another transaction while the cutover starts: the
    cutover waits for it, then sees HA and is refused; after the cutover,
    HA cannot be switched on;
  * a type set back to 'legacy' in another transaction while the cutover
    starts: the same - waits, sees it, refused; after the cutover refused;
  * a durable operation between its T1 and its end (a reseller-credit hold)
    stops the cutover; once it completed, the cutover goes through and the
    hold was captured in its own epoch;
  * the joint activation while a sale's T_final is in flight: the
    activation waits for its commit, and that sale is in the baseline - not
    lost between the baseline and the payment events;
  * the wallet rollback after the activation: refused;
  * 'required' activated between the API's receipt check and T1: T1, under
    the receipt runtime lock, refuses the unapproved priced sale;
  * HA on / a type to legacy started while the cutover holds the phase row:
    they wait, the cutover commits, they read the CURRENT phase and refuse.
(p6_ready() is patched to ready: host ownership is covered by the P6 tests.)
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
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-cutover-races-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import bot_auth, ha_fencing, loyalty_activation, provisioning_contracts as contracts
from app.services import provisioning_control as control, provisioning_durable as durable
from app.services import provisioning_parent_execute as execute, provisioning_readiness, provisioning_schema
from app.services import receipt_void_schema, user_ops, wallet_accounts, wallet_cutover, wallet_service
from app.services.provisioning_host import HostIdentity

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def ok_runner(dto):
    if (dto.params or {}).get("recovery_read"):
        return execute.remote_action.RemoteActionResult(dto.action_id, execute.remote_action.Outcome.ALREADY_PRESENT_VERIFIED,
                                                        write_attempted=False)
    return execute.remote_action.RemoteActionResult(dto.action_id, execute.remote_action.Outcome.SUCCEEDED,
                                                    write_attempted=True)


def detail(exc):
    value = exc.detail
    if isinstance(value, dict):
        return value.get("error"), sorted(b["code"] for b in value.get("blockers", []))
    return value


def scenario(label, engine):
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    check(L + "schemas ready", (receipt_void_schema.bootstrap(engine, Session)["ready"],
                                provisioning_schema.bootstrap(engine, Session)["ready"]), (True, True))
    host = HostIdentity(("e1" if label == "sqlite" else "e2") * 32, str(uuid.uuid4()))
    durable.read_identity = lambda: host
    with Session() as db:
        row = db.get(mp.ProvisioningRuntimeState, 1)
        row.owner_state, row.owner_host_id, row.owner_boot_id = "active", host.host_id, host.boot_id
        row.owner_claimed_at = row.owner_heartbeat_at = row.lock_verified_at = dt.datetime.utcnow()
        row.ownership_epoch, row.gate_mode = 1, "enforced"
        row.lock_backend = "flock" if engine.dialect.name == "sqlite" else "flock+get_lock"
        db.add(models.PanelSettings(id=1, loyalty_purchase_threshold=3, loyalty_reward_credit=100))
        root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)
        reseller = models.AdminUser(username="res", hashed_password="x", balance=5000)
        db.add_all([root, reseller])
        db.flush()
        node = models.Node(name="mt", type=models.NodeType.mikrotik, enabled=True, mt_host="10.31.0.1",
                           mt_wireguard_interface="wg0", mt_client_subnet="10.71.0.0/24")
        db.add(node)
        db.flush()
        db.add(mp.ProvisioningNodeContract(node_id=node.id, backend="mikrotik_wg", state="ready",
            adapter_version=contracts.adapter_version("mikrotik_wg"), config_fingerprint=contracts.config_fingerprint(node),
            server_fingerprint="b" * 64, contract='{"not_exist":[]}', verified_by_admin_id=root.id,
            verified_at=dt.datetime.utcnow()))
        db.add(models.AdminNodeAccess(admin_id=reseller.id, node_id=node.id))
        owned = models.Package(name="Owned", quota_gb=5, duration_days=30, price=500, cooperation_price=300,
                               owner_admin_id=reseller.id)
        shared = models.Package(name="Shared", quota_gb=5, duration_days=30, price=400)
        db.add_all([owned, shared])
        db.flush()
        for package in (owned, shared):
            db.add(models.PackageConnection(package_id=package.id, node_id=node.id, protocol="wireguard"))
        wallet_accounts.create_user_with_wallet(db, username="cust", telegram_id=902, owner_admin_id=reseller.id)
        wallet_accounts.create_user_with_wallet(db, username="plain", telegram_id=903, balance=5000)
        db.commit()
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", key_identity_ready=True, key_identity_failure=None))
        for kind in mp.OPERATION_TYPES:
            db.get(mp.ProvisioningTypeMode, kind).mode = "durable"
        db.commit()
        OWNED, SHARED, ROOT, RESELLER = owned.id, shared.id, root.id, reseller.id
    principal = bot_auth.BotPrincipal.internal(None, scope_enforced=False)

    def cutover():
        with Session() as db:
            try:
                return wallet_cutover.cutover(db)["phase"]
            except HTTPException as exc:
                return detail(exc)

    def threaded(target):
        out = {}
        thread = threading.Thread(target=lambda: out.update(result=target()))
        thread.start()
        return thread, out

    # ------------------------------------------------------------------ HA on vs the cutover
    holding, release = threading.Event(), threading.Event()

    def ha_on():
        with Session() as db:
            ha_fencing.refuse_enabling(db)               # the phase row, exclusively, to the commit
            db.get(models.PanelSettings, 1).ha_enabled = True
            db.flush()
            holding.set()
            release.wait(30)
            db.commit()
        return "ha on"

    ha_thread, _ = threaded(ha_on)
    holding.wait(15)
    cut_thread, cut = threaded(cutover)
    time.sleep(1.5)
    waited = cut_thread.is_alive()
    release.set()
    ha_thread.join(30)
    cut_thread.join(60)
    check(L + "HA switched on while the cutover starts: the cutover waits, then sees HA and is refused",
          (waited, cut.get("result")), (True, ("wallet_cutover_blocked", ["ha_requires_external_fencing"])))
    with Session() as db:
        db.get(models.PanelSettings, 1).ha_enabled = False
        db.commit()

    # ------------------------------------------------------------------ a type back to legacy vs the cutover
    holding.clear(), release.clear()
    real_lock = wallet_service.lock_phase_for_change

    def paused_lock(db):
        phase = real_lock(db)
        holding.set()
        release.wait(30)
        return phase

    def to_legacy():
        with patch.object(wallet_service, "lock_phase_for_change", side_effect=paused_lock):
            return control.change_type_mode(Session, ROOT, "add_connection", "legacy")["mode"]

    type_thread, typed = threaded(to_legacy)
    holding.wait(15)
    cut_thread, cut = threaded(cutover)
    time.sleep(1.5)
    waited = cut_thread.is_alive()
    release.set()
    type_thread.join(30)
    cut_thread.join(60)
    check(L + "a type set back to legacy while the cutover starts: the cutover waits, sees it, refused",
          (typed.get("result"), waited, cut.get("result")),
          ("legacy", True, ("wallet_cutover_blocked", ["provisioning_types_not_durable"])))
    with Session() as db:
        db.get(mp.ProvisioningTypeMode, "add_connection").mode = "durable"
        db.commit()

    # ------------------------------------------------------------------ 'required' lands between the API check and T1
    real_guard = bot_router._require_payment_evidence

    def guard_then_activation(db, payload, **kwargs):
        real_guard(db, payload, **kwargs)              # passes: still 'shadow' here
        with Session() as other:                       # the joint activation commits in between
            other.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1))
            other.commit()

    with Session() as db:
        operations_before = db.query(mp.ProvisioningOperation).count()
        holds_before = db.query(mp.PaymentReservation).count()
    with Session() as db, patch.object(bot_router, "_require_payment_evidence", side_effect=guard_then_activation), \
            patch.object(execute.remote_runner, "run_action", side_effect=ok_runner), \
            patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")):
        try:
            bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
                package_id=OWNED, paid_amount=500, payment_method="card"), db=db, owner_admin_id=None,
                principal=principal, x_idempotency_key=str(uuid.uuid4()))
            raced = "sold"
        except HTTPException as exc:
            raced = exc.detail
    with Session() as db:
        created = (db.query(mp.ProvisioningOperation).count() - operations_before,
                   db.query(mp.PaymentReservation).count() - holds_before)
        db.execute(rv.receipt_approval_runtime_state.update().values(
            registration_mode="shadow", loyalty_timing_mode="legacy_activation"))
        db.commit()
    check(L + "'required' activated between the API check and T1: T1 refuses the unapproved sale, nothing made",
          (raced, created), ("approval_uuid_required", (0, 0)))

    # ------------------------------------------------------------------ an operation in flight vs the cutover
    in_remote, go_on = threading.Event(), threading.Event()

    def blocking_runner(dto):
        in_remote.set()
        go_on.wait(30)
        return ok_runner(dto)

    def buy():
        with Session() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")):
            try:
                bot_router.purchase_package("cust", schemas.BotPurchasePackageRequest(
                    package_id=OWNED, paid_amount=500, payment_method="card"), db=db, owner_admin_id=None,
                    principal=principal, x_idempotency_key=str(uuid.uuid4()))
                return "ok"
            except HTTPException as exc:
                return detail(exc)

    with patch.object(execute.remote_runner, "run_action", side_effect=blocking_runner):
        buy_thread, bought = threaded(buy)
        in_remote.wait(30)
        with Session() as db:
            hold = db.execute(select(mp.PaymentReservation.payer_kind, mp.PaymentReservation.state)).all()
        refused = cutover()
        go_on.set()
        buy_thread.join(60)
    check(L + "a reseller-credit hold of an operation in flight: the cutover is refused",
          (hold, refused), ([("reseller_credit", "reserved")],
                            ("wallet_cutover_blocked", ["provisioning_operations_in_flight"])))
    with Session() as db:
        captured = db.execute(select(mp.PaymentReservation.state)).scalars().all()
    check(L + "...the operation then ends in its own epoch: its hold captured", (bought.get("result"), captured),
          ("ok", ["captured"]))
    backfilling, finish = threading.Event(), threading.Event()
    real_backfill = wallet_cutover._backfill

    def paused_backfill(db):
        result = real_backfill(db)
        backfilling.set()
        finish.wait(30)
        return result

    def late_ha():
        with Session() as db:
            try:
                ha_fencing.refuse_enabling(db)        # waits for the cutover's phase lock
                db.get(models.PanelSettings, 1).ha_enabled = True
                db.commit()
                return "ha on"
            except HTTPException as exc:
                db.rollback()
                return exc.detail

    def late_type():
        try:
            return control.change_type_mode(Session, ROOT, "add_connection", "legacy")["mode"]
        except HTTPException as exc:
            return exc.detail

    with patch.object(wallet_cutover, "_backfill", side_effect=paused_backfill):
        cut_thread, cut = threaded(cutover)
        backfilling.wait(30)                          # the cutover holds the phase row
        ha_thread, ha_late = threaded(late_ha)
        type_thread, type_late = threaded(late_type)
        time.sleep(1.5)
        waiting = (ha_thread.is_alive(), type_thread.is_alive())
        finish.set()                                  # the cutover commits 'enforced' ...
        cut_thread.join(60)
        ha_thread.join(60)                            # ... then HA and the type change leave their wait
        type_thread.join(60)
    with Session() as db:
        after = (bool(db.get(models.PanelSettings, 1).ha_enabled), db.get(mp.ProvisioningTypeMode, "add_connection").mode)
    check(L + "with nothing in flight the cutover goes through", cut.get("result"), "enforced")
    check(L + "HA and a legacy type started during it waited, then saw 'enforced' (current read) and were refused",
          (waiting, ha_late.get("result"), type_late.get("result"), after),
          ((True, True), "ha_requires_external_fencing", "wallet_enforced_requires_durable", (False, "durable")))
    with Session() as db:
        try:
            ha_fencing.refuse_enabling(db)
            ha = "allowed"
        except HTTPException as exc:
            ha = exc.detail
        db.rollback()
    try:
        control.change_type_mode(Session, ROOT, "add_connection", "legacy")
        legacy = "allowed"
    except HTTPException as exc:
        legacy = exc.detail
    check(L + "after the cutover: HA and a legacy type are refused", (ha, legacy),
          ("ha_requires_external_fencing", "wallet_enforced_requires_durable"))

    # ------------------------------------------------------------------ the activation vs a T_final in flight
    counted, resume = threading.Event(), threading.Event()
    real_count = user_ops._count_legacy_purchase

    def paused_count(db, user):
        real_count(db, user)
        counted.set()
        resume.wait(30)

    def wallet_sale():
        with Session() as db, patch.object(user_ops, "get_connection_share", side_effect=RuntimeError("no node")):
            try:
                bot_router.purchase_package("plain", schemas.BotPurchasePackageRequest(
                    package_id=SHARED, paid_amount=400, payment_method="wallet", charge_wallet=True), db=db,
                    owner_admin_id=None, principal=principal, x_idempotency_key=str(uuid.uuid4()))
                return "ok"
            except HTTPException as exc:
                return detail(exc)

    def activate():
        with Session() as db:
            try:
                return loyalty_activation.activate(db, ROOT)["activated"]
            except HTTPException as exc:
                return detail(exc)

    with Session() as db:
        from app.services import product_decisions
        product_decisions.confirm(db, ROOT, version=product_decisions.VERSION, content_hash=product_decisions.CONTENT_HASH)
    with patch.object(execute.remote_runner, "run_action", side_effect=ok_runner), \
            patch.object(user_ops, "_count_legacy_purchase", side_effect=paused_count):
        sale_thread, sold = threaded(wallet_sale)
        counted.wait(30)                              # inside T_final, the purchase counted, not committed
        act_thread, activated = threaded(activate)
        time.sleep(1.5)
        waited = act_thread.is_alive()
        resume.set()
        sale_thread.join(60)
        act_thread.join(60)
    with Session() as db:
        plain = db.query(models.User).filter_by(username="plain").one()
        baseline = db.execute(select(rv.loyalty_user_baselines.c.purchase_count_baseline).where(
            rv.loyalty_user_baselines.c.user_id == plain.id)).scalar()
        events = db.execute(select(rv.loyalty_purchase_events.c.id).where(
            rv.loyalty_purchase_events.c.user_id == plain.id)).all()
    check(L + "the activation while a sale's T_final is in flight: it waits for that commit",
          (sold.get("result"), waited, activated.get("result")), ("ok", True, True))
    check(L + "...and that sale is in the baseline (P=1), not lost and not counted twice",
          (baseline, len(events), plain.purchase_count), (1, 0, 1))

    # ------------------------------------------------------------------ rollback after the activation
    with Session() as db:
        try:
            wallet_cutover.rollback(db)
            rolled = "rolled back"
        except HTTPException as exc:
            rolled = exc.detail
        phase = db.execute(select(rv.wallet_runtime_state.c.phase)).scalar()
    check(L + "the wallet rollback after the activation: refused, the wallet stays enforced",
          (rolled, phase), ("wallet_rollback_after_activation", "enforced"))


with contextlib.ExitStack() as cleanup, patch.object(provisioning_readiness, "p6_ready", return_value=(True, [])):
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="cutover_races_"))
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
