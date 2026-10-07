"""L2 DB-only cores: no hidden commit/network, caller-owned rollback.

SQLite locally; a fresh, disposable MariaDB database is mandatory in CI.
"""
import ast
import datetime as dt
import inspect
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "")])
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import receipt_void_schema, user_ops
from app.services import bot_auth, receipt_approval_registration as registration
from app.services import receipt_approval_runtime as runtime, receipt_approval_intent as intent

failed = []
GB = 1024 ** 3


def check(name, got, expected=True):
    print(("PASS  " if got == expected else "FAIL  ") + name)
    if got != expected:
        failed.append((name, got, expected))


def scenario(engine, label):
    models.Base.metadata.create_all(engine)
    Factory = sessionmaker(bind=engine, autoflush=False)
    check(label + " schema", receipt_void_schema.bootstrap(engine, Factory)["ready"])
    db = Factory()
    settings = models.PanelSettings(id=1, loyalty_purchase_threshold=0,
                                    referral_referrer_reward_credit=30,
                                    referral_new_user_reward_credit=20,
                                    referral_referrer_reward_gb=1,
                                    referral_new_user_reward_gb=1)
    referrer = models.User(username="ref", referral_code="CORECODE", total_quota_bytes=GB, balance=0)
    code = models.DiscountCode(code="CORE", kind="fixed", value=10, max_uses=1, used_count=0)
    db.add_all([settings, referrer, code,
                models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)])
    db.commit()
    ref_id, code_id = referrer.id, code.id
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(True))

    user = user_ops.create_user_record_core(db, "rolled_back", quota_gb=2)
    user_id = user.id
    check(label + " creation core has no commit", commits, [])
    check(label + " creation assigns an ID", user_id is not None)
    db.rollback()
    check(label + " creation rollback removes User", db.query(models.User).filter_by(username="rolled_back").count(), 0)

    user = user_ops.create_user_record(db, "buyer", quota_gb=2, telegram_id=222)
    check(label + " legacy creation commits once", len(commits), 1)
    buyer_id = user.id
    purchase = models.Purchase(user_id=buyer_id, quota_bytes=3 * GB)
    db.add(purchase)
    db.commit()
    purchase_id = purchase.id
    commits.clear()
    evidence = []
    result = user_ops.apply_referral_code_core(db, user, "CORECODE", evidence)
    check(label + " referral core succeeds without commit", (result, commits), ((True, ""), []))
    check(label + " actual Purchase destination is returned as evidence",
          [(type(resource).__name__, ev.target_id) for kind, key, resource, ev in evidence if kind == "quota_reward_granted"],
          [("User", ref_id), ("Purchase", purchase_id)])
    db.flush()
    db.rollback()
    db.expire_all()
    check(label + " rollback removes every referral mutation",
          (db.get(models.User, buyer_id).balance, db.get(models.User, buyer_id).referred_by_id,
           db.get(models.User, ref_id).balance, db.get(models.Purchase, purchase_id).quota_bytes),
          (0, None, 0, 3 * GB))
    user = db.get(models.User, buyer_id)
    result = user_ops.apply_referral_code(db, user, "CORECODE")
    check(label + " legacy referral commits once", (result[0], len(commits)), (True, 1))
    commits.clear()

    evidence = []
    result = user_ops.redeem_discount_code_core(db, "CORE", "buyer", 100, evidence_sink=evidence)
    check(label + " discount core does not commit", (result, commits), ((True, "", 10), []))
    check(label + " discount evidence is exact", (evidence[0][1].used_count_before, evidence[0][1].used_count_after), (0, 1))
    db.rollback()
    check(label + " discount rollback restores capacity and redemption",
          (db.get(models.DiscountCode, code_id).used_count, db.query(models.DiscountCodeRedemption).count()), (0, 0))
    result = user_ops.redeem_discount_code(db, "CORE", "buyer", 100)
    check(label + " legacy discount commits once", (result[0], len(commits)), (True, 1))
    commits.clear()
    # Simulate a validation that ran before another transaction consumed the
    # slot. UPDATE must independently enforce capacity and prior use.
    original = user_ops.validate_discount_code
    user_ops.validate_discount_code = lambda *args, **kwargs: (True, "", 10)
    try:
        result = user_ops.redeem_discount_code_core(db, "CORE", "other", 100)
        check(label + " stale validation cannot exceed capacity", result[0], False)
        code = db.get(models.DiscountCode, code_id)
        code.max_uses = None
        db.commit()
        commits.clear()
        result = user_ops.redeem_discount_code_core(db, "CORE", "buyer", 100)
        check(label + " stale validation cannot redeem twice for same username", result[0], False)
    finally:
        user_ops.validate_discount_code = original
    check(label + " refused core never commits", commits, [])
    db.rollback()
    check(label + " no extra redemption", db.query(models.DiscountCodeRedemption).count(), 1)

    renewable = user_ops.create_user_record(db, "renewable", quota_gb=2, expire_days=3)
    rid = renewable.id
    p = db.get(models.Purchase, purchase_id)
    p.expire_at = dt.datetime.utcnow() + dt.timedelta(days=3)
    db.commit()
    commits.clear()
    evidence, reconcile = [], []
    user_ops.renew_user_core(db, renewable, add_gb=1, add_days=2,
                             evidence_sink=evidence, reconciliation_sink=reconcile)
    user_ops.renew_purchase_core(db, p, add_gb=1, add_days=2,
                                 evidence_sink=evidence, reconciliation_sink=reconcile)
    check(label + " reserved renewals have no commit or remote reconciliation",
          (commits, reconcile, [ev.renewal_mode for ev in evidence]), ([], [], ["reserved", "reserved"]))
    db.flush()
    db.rollback()
    check(label + " reservation mutations roll back together",
          (db.get(models.User, rid).reserved_quota_bytes, db.get(models.Purchase, purchase_id).reserved_quota_bytes),
          (None, None))
    renewable = db.get(models.User, rid)
    p = db.get(models.Purchase, purchase_id)
    for resource in (renewable, p):
        resource.expire_at = dt.datetime.utcnow() - dt.timedelta(days=1)
        resource.status = models.UserStatus.expired
    db.commit()
    commits.clear()
    evidence, reconcile = [], []
    user_ops.renew_user_core(db, renewable, add_gb=4, add_days=5,
                             evidence_sink=evidence, reconciliation_sink=reconcile)
    user_ops.renew_purchase_core(db, p, add_gb=4, add_days=5,
                                 evidence_sink=evidence, reconciliation_sink=reconcile)
    check(label + " immediate reset returns reconciliation targets, never calls them",
          (commits, [type(r).__name__ for r in reconcile], [ev.renewal_mode for ev in evidence]),
          ([], ["User", "Purchase"], ["immediate_reset", "immediate_reset"]))
    db.flush()
    db.rollback()
    check(label + " immediate reset rollback restores both statuses",
          (db.get(models.User, rid).status, db.get(models.Purchase, purchase_id).status),
          (models.UserStatus.expired, models.UserStatus.expired))
    old_user_reconcile, old_purchase_reconcile = user_ops.reconcile_user_connections, user_ops.reconcile_purchase_connections
    called = []
    user_ops.reconcile_user_connections = lambda *args: called.append("User")
    user_ops.reconcile_purchase_connections = lambda *args: called.append("Purchase")
    try:
        user_ops.renew_user(db, db.get(models.User, rid), add_gb=4, add_days=5)
        user_ops.renew_purchase(db, db.get(models.Purchase, purchase_id), add_gb=4, add_days=5)
    finally:
        user_ops.reconcile_user_connections, user_ops.reconcile_purchase_connections = old_user_reconcile, old_purchase_reconcile
    check(label + " legacy renewals reconcile once and commit once each", (called, len(commits)), (["User", "Purchase"], 2))

    package = models.Package(name="atomic", quota_gb=4, duration_days=5, price=100)
    atomic_code = models.DiscountCode(code="ATOMIC", kind="fixed", value=10, used_count=0)
    db.add_all([package, atomic_code])
    db.commit()
    atomic_id = atomic_code.id
    runtime.set_requested_mode(db, registration_mode="shadow")
    db.commit()
    principal = bot_auth.BotPrincipal.internal(None)
    approval = registration.begin(db, principal, intent.ApprovalIntent(
        pending_source_instance_id="cores-" + label, pending_local_id=1,
        kind="renew", target_username="buyer", amount=90, package_id=package.id,
        renew_purchase_id=purchase_id, list_price=100, discount_code="ATOMIC", claimed_telegram_id=222),
        approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
    observed = []

    def crash_at_commit(session):
        if session.in_nested_transaction():
            return
        observed.append((session.query(models.DiscountCodeRedemption).filter_by(code_id=atomic_id).count(),
                         len(session.execute(select(rv.receipt_approval_effects.c.id).where(
                             rv.receipt_approval_effects.c.approval_uuid == approval)).all())))
        raise RuntimeError("injected commit failure")

    event.listen(db, "before_commit", crash_at_commit)
    try:
        try:
            bot_router.redeem_discount(schemas.DiscountRedeemRequest(
                code="ATOMIC", username="buyer", package_price=100, approval_uuid=approval), db=db, principal=principal)
            check(label + " injected crash occurred", False)
        except RuntimeError:
            db.rollback()
    finally:
        event.remove(db, "before_commit", crash_at_commit)
    check(label + " discount and effect exist in the same transaction before commit", observed, [(1, 1)])
    check(label + " commit failure leaves no discount, redemption or effect",
          (db.get(models.DiscountCode, atomic_id).used_count,
           db.query(models.DiscountCodeRedemption).filter_by(code_id=atomic_id).count(),
           len(db.execute(select(rv.receipt_approval_effects.c.id).where(
               rv.receipt_approval_effects.c.approval_uuid == approval)).all())), (0, 0, 0))
    response = bot_router.redeem_discount(schemas.DiscountRedeemRequest(
        code="ATOMIC", username="buyer", package_price=100, approval_uuid=approval), db=db, principal=principal)
    check(label + " retry records discount and effect once", (response.valid, db.get(models.DiscountCode, atomic_id).used_count,
          len(db.execute(select(rv.receipt_approval_effects.c.id).where(
              rv.receipt_approval_effects.c.approval_uuid == approval)).all())), (True, 1, 1))
    race_code = models.DiscountCode(code="RACE", kind="fixed", value=10, max_uses=1, used_count=0)
    db.add(race_code)
    db.commit()
    race_id = race_code.id
    db.close()
    barrier = threading.Barrier(2)
    local = threading.local()
    original = user_ops.validate_discount_code
    results, errors = [], []

    def synchronized_validation(*args, **kwargs):
        result = original(*args, **kwargs)
        if not getattr(local, "validated", False):
            local.validated = True
            barrier.wait(timeout=10)
        return result

    def competitor(username):
        session = Factory()
        try:
            for attempt in range(3):
                try:
                    results.append(user_ops.redeem_discount_code(session, "RACE", username, 100)[0])
                    break
                except OperationalError:
                    session.rollback()
                    if attempt == 2:
                        raise
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            session.close()

    user_ops.validate_discount_code = synchronized_validation
    threads = [threading.Thread(target=competitor, args=(username,)) for username in ("race1", "race2")]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
    finally:
        user_ops.validate_discount_code = original
    check(label + " two real sessions finish without errors", (errors, any(t.is_alive() for t in threads)), ([], False))
    verify = Factory()
    try:
        check(label + " two concurrent validations consume only one capacity slot",
              (sum(results), verify.get(models.DiscountCode, race_id).used_count,
               verify.query(models.DiscountCodeRedemption).filter_by(code_id=race_id).count()), (1, 1, 1))
    finally:
        verify.close()


for core in (user_ops.create_user_record_core, user_ops.apply_referral_code_core,
             user_ops.redeem_discount_code_core, user_ops.renew_user_core, user_ops.renew_purchase_core):
    calls = [n.func.attr if isinstance(n.func, ast.Attribute) else n.func.id
             for n in ast.walk(ast.parse(inspect.getsource(core)))
             if isinstance(n, ast.Call) and isinstance(n.func, (ast.Attribute, ast.Name))]
    check(core.__name__ + " no transaction boundary or remote dispatch",
          set(calls) & {"commit", "rollback", "close", "provision_connection", "client_for_node",
                        "reconcile_user_connections", "reconcile_purchase_connections"}, set())

with tempfile.TemporaryDirectory(prefix="um-mutation-cores-") as directory:
    engine = create_engine("sqlite:///" + os.path.join(directory, "test.db"))
    try:
        scenario(engine, "sqlite")
    finally:
        engine.dispose()

url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario(engine, "mariadb")
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    failed.append(("CI requires real MariaDB", "missing URL", "URL"))
else:
    print("SKIP  MariaDB locally; mandatory in CI")
check("no outgoing node connection", _no_network.attempts, [])
for failure in failed:
    print(failure)
sys.exit(bool(failed))
