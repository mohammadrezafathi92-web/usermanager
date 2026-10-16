"""Frozen-contract cases of the receipt void not covered elsewhere, SQLite
AND real MariaDB (CI). Fake runner; no network beyond the scratch database.

  RV-118  new_user with three connections: each node removal called once
  RV-55   the second connection's removal unverified: cleanup_required,
          steps 5+ not run, nothing deleted; retry continues
  RV-122  a removal answered "already absent" -> delete_idempotently_absent
  RV-119  db_delete's transaction fails: rolled back, retried with NO new
          remote call
  RV-47   void of a top-up: the customer stays, fraud hold, top-ups still
          accepted and settle the debt; purchases refused while held
  RV-58   two superadmins execute the same void at once: one operation,
          both answered with it
  RV-138  a referral quota reward whose target changed since: ledger step
          blocked -> cleanup_required -> force -> not_applicable, completed
"""
import contextlib
import os
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-void-contract-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import func, select

from app import models, models_receipt_void as rv
from app.services import receipt_void_engine as void_engine, receipt_void_operator as operator
from app.services import user_ops, wallet_accounts, wallet_lots
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def peers(env, name):
    with env.Session() as db:
        user = db.query(models.User).filter_by(username=name).one()
        return {c.id: c.wg_peer_name for c in db.query(models.Connection).filter_by(user_id=user.id)
                .order_by(models.Connection.id)}


def calls(peer):
    return sum(1 for _kind, name in fixture.CALLS if name == peer)


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)

        # ============================================================ RV-118 / RV-55 / RV-122
        first = env.new_user("multi", 7201)
        env.purchase("multi", 7201)
        env.purchase("multi", 7201)
        connections = peers(env, "multi")
        ids = list(connections)
        check(L + "three connections on the account", len(ids), 3)
        fixture.ABSENT["unverified_peers"] = {connections[ids[1]]}
        fixture.ABSENT["idempotent_peers"] = {connections[ids[2]]}
        shown = env.preview(first["approval_uuid"])
        done = env.void(shown["preview_token"], "multi")
        steps = env.steps_of(done["operation_id"])
        check(L + "RV-55 the 2nd connection unverified: cleanup_required, its verify blocked, steps 5+ not run",
              (done["state"], steps[f"verify:{ids[0]}"], steps[f"verify:{ids[1]}"], steps["db_delete"][0],
               steps["wallet_reversal"][0], env.user_id("multi") is not None),
              ("cleanup_required", ("done", "verified_absent"), ("blocked", "unverified"), "pending", "pending", True))
        fixture.ABSENT["unverified_peers"] = set()
        retried = fixture.code(lambda: operator.retry(env.Session, env.root, done["operation_id"]))
        steps = env.steps_of(done["operation_id"])
        check(L + "...retry continues to completion; RV-122 the 'already absent' answer is delete_idempotently_absent",
              (retried["state"], steps[f"verify:{ids[2]}"], env.user_id("multi")),
              ("completed", ("done", "delete_idempotently_absent"), None))
        check(L + "RV-118 each node removal called once (the retried unverified one twice)",
              [calls(connections[i]) for i in ids], [1, 2, 1])
        fixture.ABSENT["idempotent_peers"] = set()

        # ============================================================ RV-119
        item = env.new_user("dbfail", 7202)
        peer = list(peers(env, "dbfail").values())[0]
        with patch.object(user_ops, "finalize_user_deletion_after_deprovision", side_effect=RuntimeError("db down")):
            failed = env.void(env.preview(item["approval_uuid"])["preview_token"], "dbfail")
        removed_once = calls(peer)
        check(L + "RV-119 db_delete fails: rolled back, the account stays, the void not finished",
              (failed["state"] != "completed", env.user_id("dbfail") is not None, removed_once), (True, True, 1))
        with env.Session() as db:
            db.execute(rv.wallet_operation_steps.update().values(next_retry_at=None))
            from app import models_provisioning as mp
            db.execute(mp.ProvisioningOperation.__table__.update().values(next_retry_at=None))
            db.commit()
        if failed["state"] == "cleanup_required":
            fixture.code(lambda: operator.retry(env.Session, env.root, failed["operation_id"]))
        for _ in range(4):
            operator.recover_due(env.Session)
            from app.services import provisioning_durable
            provisioning_durable.recover_due(env.Session)
        check(L + "...retried: completed with NO new remote call",
              (env.approval(item["approval_uuid"])["state"], env.user_id("dbfail"), calls(peer)), ("voided", None, 1))

        # ============================================================ RV-47
        with env.Session() as db:
            wallet_accounts.create_user_with_wallet(db, username="topv", telegram_id=7203)
            db.commit()
        top = env.topup("topv", 1500)
        env.wallet_sale("topv")
        done = env.void(env.preview(top["approval_uuid"])["preview_token"], "topv")

        def owed():
            with env.Session() as db:
                identity = db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
                    rv.wallet_accounts.c.user_id == env.user_id("topv"))).scalar()
                return wallet_lots.open_debt(db, identity)

        before = owed()
        again = env.topup("topv", 400)
        try:
            refused = env.purchase("topv", 7203)
            refused = env.approval(refused["approval_uuid"])["state"] if refused.get("approval_uuid") else refused
        except Exception as exc:  # noqa: BLE001 - the registration refuses
            refused = getattr(exc, "code", type(exc).__name__)
        check(L + "RV-47 top-up void: the customer stays; a later top-up is accepted and settles the debt",
              (done["state"], env.user_id("topv") is not None, before, env.approval(again["approval_uuid"])["state"],
               owed()), ("completed", True, 1000, "completed", 600))
        check(L + "...a purchase is refused while the fraud hold stands", refused != "completed", True)

        # ============================================================ RV-58
        with env.Session() as db:
            second = models.AdminUser(username="root2", hashed_password="x", is_superadmin=True, telegram_id=1003)
            db.add(second)
            db.commit()
            db.refresh(second)
            db.expunge(second)
        twice = env.new_user("twice", 7204)
        token = env.preview(twice["approval_uuid"])["preview_token"]
        answers, barrier = [], threading.Barrier(2)

        def run_as(admin):
            barrier.wait()
            answers.append(fixture.code(lambda: void_engine.execute(
                env.Session, admin, preview_token=token, typed_username="twice", reason="fake receipt",
                danger_ack=True, ip="203.0.113.8")))

        threads = [threading.Thread(target=run_as, args=(who,)) for who in (env.root, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        with env.Session() as db:
            count = db.execute(select(func.count()).select_from(rv.wallet_operations).where(
                rv.wallet_operations.c.business_key == twice["approval_uuid"])).scalar_one()
        check(L + "RV-58 two superadmins at once: one operation, both answered with it",
              (count, len({a.get("operation_id") for a in answers if isinstance(a, dict)}),
               all(isinstance(a, dict) for a in answers)), (1, 1, True))

        # ============================================================ RV-138
        with env.Session() as db:
            qref = wallet_accounts.create_user_with_wallet(db, username="qref", telegram_id=7205)
            qref.referral_code = "QREF"
            db.commit()
        env.purchase("qref", 7205)                          # exactly one service: the quota destination
        with env.Session() as db:
            db.query(models.PanelSettings).update({"referral_referrer_reward_gb": 1})
            db.commit()
        referred = env.new_user("qnew", 7206, referral_code="QREF")
        with env.Session() as db:
            db.query(models.PanelSettings).update({"referral_referrer_reward_gb": 0})
            effect = db.execute(select(rv.receipt_approval_effects).where(
                rv.receipt_approval_effects.c.approval_uuid == referred["approval_uuid"],
                rv.receipt_approval_effects.c.effect_type == "quota_reward_granted")).mappings().first()
            target = db.get(models.Purchase, int(effect["resource_id"])) if effect else None
            edited = None
            if target is not None:
                target.quota_bytes = int(target.quota_bytes) + 3 * 1024 ** 3     # an admin edit since the reward
                edited = int(target.quota_bytes)
            db.commit()
        check(L + "a referral quota reward on the referrer's one service", effect is not None and effect["resource_type"],
              "Purchase")
        done = env.void(env.preview(referred["approval_uuid"])["preview_token"], "qnew")
        steps = env.steps_of(done["operation_id"])
        with env.Session() as db:
            error = next(s["error_sanitized"] for s in void_engine._steps(db, done["operation_id"])
                         if s["step_key"] == "ledger_reversal")
        check(L + "RV-138 quota changed since the reward: ledger step blocked, cleanup_required",
              (done["state"], steps["ledger_reversal"][0], error), ("cleanup_required", "failed", "quota_changed_since_reward"))
        forced = fixture.code(lambda: operator.force_step(env.Session, env.root, done["operation_id"],
                                                          step_key="ledger_reversal", reason="admin edit", danger_ack=True))
        with env.Session() as db:
            state = db.execute(select(rv.receipt_approval_effects.c.reversal_state).where(
                rv.receipt_approval_effects.c.id == effect["id"])).scalar()
            quota = int(db.get(models.Purchase, int(effect["resource_id"])).quota_bytes)
        check(L + "...after force: not_applicable, the quota untouched, the void completed",
              (forced["state"], state, quota == edited), ("completed", "not_applicable", True))


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="void_contract_"))
    sqlite = fixture.sqlite_engine(os.path.join(folder, "t.db"))
    cleanup.callback(sqlite.dispose)
    scenario("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
        fixture.CALLS.clear()
        scenario("mariadb", maria)
    elif os.environ.get("CI"):
        check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
    else:
        print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
