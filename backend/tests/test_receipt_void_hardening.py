"""Receipt void hardening (review of PR #139), SQLite AND real MariaDB (CI).
Fake runner; no network beyond the scratch database.

  flags     a claim and set_flag serialize: execute/worker switched off
            before the claim -> 404, nothing claimed; switched off DURING a
            claim -> waits for its commit, then the worker cannot go off.
  hash      a healthy approval completed on the account after the preview
            (renewal: same resource ids; purchase: a new one), or a purchase
            changed -> 409 preview_state_changed; the fresh preview lists it.
  replay    the exact request after completion, with execute off, after the
            token expired -> the same operation; another reason -> 409.
  retry     block failed (nothing applied): an unchanged scope retries; a
            changed one -> 409 and a fresh preview re-plans the SAME
            operation for the current scope only.
  loyalty   the preview shows the reward execution reverses (a later healthy
            approval's), not the voided purchase's own; the User row lock
            serializes reconciliation with purchase recording.
  leases    two identities (referral) + the card pool: a live foreign lease
            makes the void wait (nothing failed, nothing taken over); after
            release it completes; write-off for the referrer; leases released.
            A concurrent top-up of the referrer and card payment converge.
  shapes    renew_user (legacy, registration refuses new ones) voids the
            whole account; a card whose pool reset and rotated: counters
            recomputed from events, the active-card pointer untouched.
"""
import contextlib
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
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-void-hardening-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

import datetime as dt
from sqlalchemy import func, select

from app import models, models_receipt_void as rv
from app.services import receipt_approval_intent as ri, receipt_approval_registration as registration
from app.services import receipt_void_effects as void_effects, receipt_void_engine as void_engine
from app.services import receipt_void_flags as flags, receipt_void_operator as operator
from app.services import receipt_void_runner as void_runner, receipt_void_scope as scope
from app.services import receipt_void_steps as void_steps, resource_leases, wallet_reconcile
import _receipt_void_env as fixture


def attempt(store, key, call):
    """A concurrent caller: a lease held by the void is refused as busy
    (never waited around); the caller retries it later."""
    try:
        store[key] = call()
    except resource_leases.LeaseBusy:
        store[key] = "busy"

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def operations(env, approval_uuid):
    with env.Session() as db:
        return db.execute(select(func.count()).select_from(rv.wallet_operations).where(
            rv.wallet_operations.c.business_key == approval_uuid)).scalar_one()


def set_flags(env, **values):
    order = [n for n in ("worker", "execute") if values.get(n) is True] + \
            [n for n in ("execute", "worker") if values.get(n) is False]     # on: worker first; off: execute first
    with env.Session() as db:
        for name in order:
            fixture.code(lambda n=name: flags.set_flag(db, n, values[n]))
        return flags.state(db)


def scenario(label, engine):
    L = f"[{label}] "
    maria = engine.dialect.name != "sqlite"
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)

        # ============================================================ 1. flags and the claim serialize
        item = env.new_user("race1", 7101)
        token = env.preview(item["approval_uuid"])["preview_token"]
        original = void_engine._claim

        def flags_off_first(*args, **kwargs):
            with env.Session() as db:                      # another admin, between the check and the claim
                flags.set_flag(db, "execute", False)
                flags.set_flag(db, "worker", False)
            return original(*args, **kwargs)

        with patch.object(void_engine, "_claim", side_effect=flags_off_first):
            refused = env.void(token, "race1")
        check(L + "execute + worker off between the flag check and the claim: 404, nothing claimed",
              (refused, operations(env, item["approval_uuid"]), env.approval(item["approval_uuid"])["state"]),
              ((404, "receipt_void_disabled"), 0, "completed"))
        check(L + "...the flags back on", set_flags(env, execute=True, worker=True),
              {"preview": True, "execute": True, "worker": True})

        inside, go = threading.Event(), threading.Event()
        preflight = scope.preflight

        def held_claim(*args, **kwargs):
            inside.set()
            go.wait(30)
            return preflight(*args, **kwargs)

        result, flagged = {}, {}
        fixture.ABSENT["mode"] = "unverified"                     # the void stays active (cleanup_required)
        token = env.preview(item["approval_uuid"])["preview_token"]
        with patch.object(scope, "preflight", side_effect=held_claim):
            claimer = threading.Thread(target=lambda: result.update(r=env.void(token, "race1")))
            claimer.start()
            inside.wait(30)

            def switch_off():
                with env.Session() as db:
                    flagged["execute"] = fixture.code(lambda: flags.set_flag(db, "execute", False))
            switcher = threading.Thread(target=switch_off)
            switcher.start()
            time.sleep(1.0)
            waited = switcher.is_alive()
            go.set()
            claimer.join(60)
            switcher.join(60)
        fixture.ABSENT["mode"] = "absent"
        check(L + "execute switched off DURING a claim: waits for the claim's commit",
              (waited, isinstance(result.get("r"), dict), flagged.get("execute", {}).get("execute")),
              (True, True, False))
        with env.Session() as db:
            worker_off = fixture.code(lambda: flags.set_flag(db, "worker", False))
        check(L + "...the claimed void is not stranded: the worker cannot go off",
              (result["r"]["state"], worker_off), ("cleanup_required", (409, "receipt_void_operations_active")))
        set_flags(env, execute=True)
        retried = fixture.code(lambda: operator.retry(env.Session, env.root, result["r"]["operation_id"]))
        check(L + "...retry completes it", retried["state"], "completed")

        # ============================================================ 2. the preview hash covers the destructive scope
        first = env.new_user("hashA", 7102)
        shown = env.preview(first["approval_uuid"])
        renewal = env.renew("hashA")
        check(L + "a healthy renewal completed after the preview (same resource ids)",
              env.approval(renewal["approval_uuid"])["state"], "completed")
        check(L + "...the old preview: 409 preview_state_changed, nothing claimed",
              (env.void(shown["preview_token"], "hashA"), operations(env, first["approval_uuid"])),
              ((409, "preview_state_changed"), 0))
        fresh = env.preview(first["approval_uuid"])
        check(L + "...the fresh preview lists it with kind, state and amount",
              [(a["approval_uuid"], a["kind"], a["state"], int(a["amount_snapshot"]))
               for a in fresh["other_approvals_on_account"]], [(renewal["approval_uuid"], "renew", "completed", 1000)])
        added = env.purchase("hashA", 7102)
        check(L + "a new purchase (a new resource) after the preview: 409",
              env.void(fresh["preview_token"], "hashA"), (409, "preview_state_changed"))
        fresh = env.preview(first["approval_uuid"])
        with env.Session() as db:
            db.query(models.Purchase).filter_by(user_id=env.user_id("hashA")).update(
                {"quota_bytes": 99 * 1024 ** 3})
            db.commit()
        check(L + "a purchase changed after the preview: 409", env.void(fresh["preview_token"], "hashA"),
              (409, "preview_state_changed"))
        fresh = env.preview(first["approval_uuid"])
        done = env.void(fresh["preview_token"], "hashA")
        check(L + "with the fresh preview: completed, the confirmed account deleted",
              (done["state"], env.user_id("hashA"), [a["approval_uuid"] for a in fresh["other_approvals_on_account"]]),
              ("completed", None, [renewal["approval_uuid"], added["approval_uuid"]]))

        # ============================================================ 3. exact replay
        set_flags(env, execute=False)
        check(L + "exact replay with execute off: the same operation",
              env.void(fresh["preview_token"], "hashA").get("operation_id"), done["operation_id"])
        set_flags(env, execute=True)
        with patch.object(scope.time, "time", return_value=scope.time.time() + scope.TOKEN_SECONDS + 60):
            check(L + "exact replay after the token expired: the same operation",
                  env.void(fresh["preview_token"], "hashA").get("operation_id"), done["operation_id"])
        check(L + "the same token with another reason: 409",
              env.void(fresh["preview_token"], "hashA", reason="another"), (409, "receipt_void_request_changed"))
        check(L + "a forged token for the approval: 409 invalid",
              env.void(fresh["preview_token"][:-4] + "beef", "hashA"), (409, "preview_token_invalid"))

        # ============================================================ 4. retry after a failed block
        def failing_block(db, ctx):
            raise RuntimeError("block failed")

        ok = env.new_user("blockok", 7103)
        with patch.object(void_steps, "block", side_effect=failing_block):
            failed = env.void(env.preview(ok["approval_uuid"])["preview_token"], "blockok")
        check(L + "block fails: operation failed, approval completed again, nothing applied",
              (failed["state"], env.approval(ok["approval_uuid"])["state"], env.user_id("blockok") is not None),
              ("failed", "completed", True))
        check(L + "...an unchanged scope: retry completes",
              fixture.code(lambda: operator.retry(env.Session, env.root, failed["operation_id"]))["state"], "completed")
        changed = env.new_user("blockc", 7104)
        shown = env.preview(changed["approval_uuid"])
        with patch.object(void_steps, "block", side_effect=failing_block):
            failed = env.void(shown["preview_token"], "blockc")
        later = env.purchase("blockc", 7104)
        check(L + "...a purchase completed between the failure and the retry: 409, still failed, nothing deleted",
              (fixture.code(lambda: operator.retry(env.Session, env.root, failed["operation_id"])),
               env.steps_of(failed["operation_id"])["block"][0], env.user_id("blockc") is not None),
              ((409, "preview_state_changed"), "failed", True))
        check(L + "...the old preview cannot re-claim it either", env.void(shown["preview_token"], "blockc"),
              {"operation_id": failed["operation_id"], "state": "failed", "replayed": True})
        fresh = env.preview(changed["approval_uuid"])
        done = env.void(fresh["preview_token"], "blockc")
        connections = [c["id"] for c in fresh["resources"]["connections"]]
        plan = env.steps_of(failed["operation_id"])
        check(L + "...a fresh preview re-plans the same operation for the confirmed scope: completed",
              (done["operation_id"], done["state"], len(connections),
               all(plan[f"verify:{c}"] == ("done", "verified_absent") for c in connections),
               [a["approval_uuid"] for a in fresh["other_approvals_on_account"]]),
              (failed["operation_id"], "completed", 2, True, [later["approval_uuid"]]))

        # ============================================================ 4b. the gap between claim and block
        def after_claim(work):
            real = void_engine._claim

            def wrapper(*args, **kwargs):
                answer = real(*args, **kwargs)        # committed: no lease, no fraud hold yet
                work()
                return answer
            return patch.object(void_engine, "_claim", side_effect=wrapper)

        gap = env.new_user("gapper", 7112)
        shown = env.preview(gap["approval_uuid"])
        removals = len([c for c in fixture.CALLS if c[0] == "absent"])
        healthy = {}
        with after_claim(lambda: healthy.update(item=env.purchase("gapper", 7112))):
            failed = env.void(shown["preview_token"], "gapper")
        with env.Session() as db:
            uid = env.user_id("gapper")
            connections = sorted(c.id for c in db.query(models.Connection).filter_by(user_id=uid))
            held = db.get(models.User, uid).purchases_blocked
            enabled = [c.enabled for c in db.query(models.Connection).filter_by(user_id=uid)]
            error = next(s["error_sanitized"] for s in void_engine._steps(db, failed["operation_id"])
                         if s["step_key"] == "block")
        check(L + "a purchase completed between claim and block: block fails before any mutation",
              (failed["state"], error, env.approval(gap["approval_uuid"])["state"],
               env.approval(healthy["item"]["approval_uuid"])["state"], held, all(enabled), len(connections)),
              ("failed", "preview_state_changed", "completed", "completed", False, True, 2))
        check(L + "...no remote deletion attempted",
              len([c for c in fixture.CALLS if c[0] == "absent"]), removals)
        check(L + "...the old preview cannot proceed (retry 409, execute replays the failed one)",
              (fixture.code(lambda: operator.retry(env.Session, env.root, failed["operation_id"])),
               env.void(shown["preview_token"], "gapper").get("state")), ((409, "preview_state_changed"), "failed"))
        fresh = env.preview(gap["approval_uuid"])
        check(L + "...the fresh preview lists the new approval and both connections",
              ([a["approval_uuid"] for a in fresh["other_approvals_on_account"]],
               sorted(c["id"] for c in fresh["resources"]["connections"])),
              ([healthy["item"]["approval_uuid"]], connections))
        done = env.void(fresh["preview_token"], "gapper")
        plan = env.steps_of(done["operation_id"])
        check(L + "...confirmed: the same operation re-planned with both connections, completed",
              (done["operation_id"], done["state"], [plan[f"verify:{c}"][0] for c in connections], env.user_id("gapper")),
              (failed["operation_id"], "completed", ["done", "done"], None))

        steady = env.new_user("steady", 7113)
        with after_claim(lambda: None):
            done = env.void(env.preview(steady["approval_uuid"])["preview_token"], "steady")
        check(L + "unchanged scope between claim and block: completes", (done["state"], env.user_id("steady")),
              ("completed", None))

        with env.Session() as db:
            from app.services import wallet_accounts
            wallet_accounts.create_user_with_wallet(db, username="gaptop", telegram_id=7114)
            db.commit()
        top = env.topup("gaptop", 1500)
        shown = env.preview(top["approval_uuid"])
        with after_claim(lambda: env.wallet_sale("gaptop")):
            failed = env.void(shown["preview_token"], "gaptop")
        with env.Session() as db:
            held = db.query(models.User).filter_by(username="gaptop").one().purchases_blocked
        check(L + "a top-up void whose money was spent between claim and block: fails, nothing applied",
              (failed["state"], held, env.approval(top["approval_uuid"])["state"]), ("failed", False, "completed"))
        fresh = env.preview(top["approval_uuid"])
        done = env.void(fresh["preview_token"], "gaptop")
        check(L + "...the fresh preview shows the new projection (debt 1000) and completes",
              (fresh["wallet"]["sources"][0]["projected_debt"], done["state"]), (1000, "completed"))

        # ============================================================ 5. loyalty: preview = execution
        bought = [env.purchase("loyal", 7004)["approval_uuid"] for _ in range(4)]
        shown = env.preview(bought[1])
        with env.Session() as db:
            rewards = {r.causation_approval_uuid: r.id for r in db.execute(select(rv.loyalty_reward_events))}
        planned = [r["id"] for r in shown["loyalty"]["rewards"]]
        loyalty_sources = [s["loyalty_reward_id"] for s in shown["wallet"]["sources"] if s["step"] == "loyalty_reconciliation"]
        check(L + "preview of purchase #2: the reward that goes is #4's (a later healthy approval), credited 100",
              (planned, loyalty_sources, [s["projected_clawback"] for s in shown["wallet"]["sources"]],
               shown["loyalty"]["active_count_after"]),
              ([rewards[bought[3]]], [rewards[bought[3]]], [100], 3))
        done = env.void(shown["preview_token"], "loyal")
        with env.Session() as db:
            voided = [r.id for r in db.execute(select(rv.loyalty_reward_events).where(
                rv.loyalty_reward_events.c.void_operation_id == done["operation_id"]))]
        check(L + "...execution reverses exactly the previewed reward", (done["state"], voided), ("completed", planned))

        # ---- the User row lock: reconciliation waits for a purchase that holds it
        with env.Session() as db:
            from app.services import wallet_accounts
            wallet_accounts.create_user_with_wallet(db, username="lockme", telegram_id=7106)
            db.commit()
        target = env.purchase("lockme", 7106)
        entered, held, release_it = threading.Event(), threading.Event(), threading.Event()
        real_lock = void_effects.lock_user

        def observed_lock(db, user_id):
            entered.set()
            held.wait(30)                       # another transaction holds the User row now
            return real_lock(db, user_id)

        def hold_user():
            with env.Session() as db:
                db.execute(select(models.User.id).where(models.User.id == env.user_id("lockme"))
                           .with_for_update()).all()
                held.set()
                release_it.wait(30)
                db.commit()

        result = {}
        token = env.preview(target["approval_uuid"])["preview_token"]
        with patch.object(void_effects, "lock_user", side_effect=observed_lock if maria else real_lock):
            voider = threading.Thread(target=lambda: result.update(r=env.void(token, "lockme")))
            voider.start()
            if maria:
                entered.wait(60)
                locker = threading.Thread(target=hold_user)
                locker.start()
                time.sleep(1.5)
                blocked = voider.is_alive()
                release_it.set()
                locker.join(30)
            voider.join(120)
        if maria:
            check(L + "reconciliation blocks on the User row another transaction holds, then continues",
                  (blocked, result["r"]["state"]), (True, "completed"))
        else:
            check(L + "reconciliation (SQLite: the single writer serializes)", result["r"]["state"], "completed")

        # ---- a purchase recorded concurrently with a void of the same customer
        with env.Session() as db:
            from app.services import wallet_accounts
            wallet_accounts.create_user_with_wallet(db, username="loyal2", telegram_id=7110)
            db.commit()
        fight = [env.purchase("loyal2", 7110)["approval_uuid"] for _ in range(3)]
        token = env.preview(fight[2])["preview_token"]
        outcome = {}
        threads = [threading.Thread(target=lambda: outcome.update(v=env.void(token, "loyal2"))),
                   threading.Thread(target=lambda: attempt(outcome, "p", lambda: env.purchase("loyal2", 7110)))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        busy = outcome.get("p") == "busy"
        if busy:
            outcome["p"] = env.purchase("loyal2", 7110)               # retried once the void released its leases
        if outcome.get("v") == (409, "preview_state_changed"):
            # The purchase committed first: the void refused the stale scope;
            # a fresh preview of it then voids against the new count.
            outcome["v"] = env.void(env.preview(fight[2])["preview_token"], "loyal2")
        with env.Session() as db:
            loyal = db.query(models.User).filter_by(username="loyal2").one()
            from app.services import loyalty_service
            state = loyalty_service.runtime_row(db)
            count = loyalty_service.active_count(db, loyal.id, int(state["completeness_generation"]))
            unbacked = db.execute(select(func.count()).select_from(rv.loyalty_reward_events).where(
                rv.loyalty_reward_events.c.user_id == loyal.id, rv.loyalty_reward_events.c.state == "active",
                rv.loyalty_reward_events.c.required_active_count > count)).scalar_one()
            problems = [p for p in wallet_reconcile.check(db)["problems"] if p.get("user_id") == loyal.id]
        check(L + "purchase recording and a void at once: no active reward above the count, caches consistent",
              (isinstance(outcome.get("v"), dict), unbacked, problems), (True, 0, []))

        # ============================================================ 6. leases: two identities + the card pool
        referral = env.new_user("refnew", 7107, referral_code="REFX", payment_card_id=env.ids.cards[0])
        env.wallet_sale("referrer")
        env.wallet_sale("referrer")
        shown = env.preview(referral["approval_uuid"])
        with env.Session() as db:
            def identity_of(name):
                return db.execute(select(rv.wallet_accounts.c.customer_identity_id).where(
                    rv.wallet_accounts.c.user_id == env.user_id(name))).scalar()
            keys = scope.lease_keys(db, referral["approval_uuid"], identity_of("refnew"))
            referrer_key = f"customer_identity:{identity_of('referrer')}"
        with env.Session() as db:
            resource_leases.begin_business(db)
            foreign = resource_leases.acquire(db, referrer_key, "op:999999:1", ttl=300)
            db.commit()
        check(L + "lease set: both identities and the card pool, sorted",
              (len(keys), keys[-1], keys == sorted(keys[:-1], key=lambda k: int(k.split(":")[1])) + keys[-1:]),
              (3, "payment_card_pool:global", True))
        waiting = env.void(shown["preview_token"], "refnew")
        steps = env.steps_of(waiting["operation_id"])
        check(L + "a live foreign lease on the referrer: the void waits (nothing failed, nothing taken over)",
              (waiting["state"], steps["block"][0], env.user_id("refnew") is not None), ("running", "pending", True))
        with env.Session() as db:
            resource_leases.release(db, foreign)
            db.commit()
        concurrent = {}

        def topup_referrer():
            attempt(concurrent, "t", lambda: env.topup("referrer", 700))

        def card_payment():
            attempt(concurrent, "c", lambda: env.purchase("victim", 7001, payment_card_id=env.ids.cards[0]))

        others = [threading.Thread(target=topup_referrer), threading.Thread(target=card_payment)]
        for thread in others:
            thread.start()
        for _ in range(6):
            operator.recover_due(env.Session)
            if env.approval(referral["approval_uuid"])["state"] == "voided":
                break
        for thread in others:
            thread.join(120)
        if concurrent.get("t") == "busy":
            concurrent["t"] = env.topup("referrer", 700)
        if concurrent.get("c") == "busy":
            concurrent["c"] = env.purchase("victim", 7001, payment_card_id=env.ids.cards[0])
        with env.Session() as db:
            writeoffs = [int(r.amount) for r in db.execute(select(rv.wallet_writeoffs).where(
                rv.wallet_writeoffs.c.wallet_operation_id == waiting["operation_id"]))]
            held = db.execute(select(func.count()).select_from(rv.resource_locks).where(
                rv.resource_locks.c.resource_key.in_(keys), rv.resource_locks.c.lease_owner.isnot(None))).scalar_one()
            problems = wallet_reconcile.check(db)["problems"]
            card_events = db.execute(select(rv.payment_card_pool_events.c.voided_at).where(
                rv.payment_card_pool_events.c.approval_uuid == referral["approval_uuid"])).scalar()
        check(L + "...after the release: voided; the referrer's spent 2000 written off; card event voided",
              (env.approval(referral["approval_uuid"])["state"], writeoffs, card_events is not None),
              ("voided", [2000], True))
        check(L + "...concurrent referrer top-up and card payment converged; every lease released; invariants hold",
              (env.approval(concurrent["t"]["approval_uuid"])["state"], env.approval(concurrent["c"]["approval_uuid"])["state"],
               held, problems), ("completed", "completed", 0, []))

        # ============================================================ 7. renew_user and a reset/rotated card
        with env.Session() as db:
            from app.services import wallet_accounts
            wallet_accounts.create_user_with_wallet(db, username="nosvc", telegram_id=7111)
            db.commit()
        with env.Session() as db:
            try:
                registration.register(db, env.internal, ri.ApprovalIntent(
                    pending_source_instance_id=f"void-{label}", pending_local_id=9001, kind="renew",
                    target_username="nosvc", amount=1000, package_id=env.ids.shared, list_price=1000),
                    approval_mode="auto")
                refusal = "registered"
            except registration.RegistrationRejected as exc:
                refusal = exc.code
            db.rollback()
        check(L + "renew_user cannot be registered any more (owner decision)", refusal, "renew_service_selection_required")
        legacy_uuid = str(uuid.uuid4())
        with env.Session() as db:
            from app.services import wallet_accounts
            legacy = wallet_accounts.create_user_with_wallet(db, username="legacyu", telegram_id=7108)
            db.flush()
            db.execute(rv.receipt_approvals.insert().values(
                approval_uuid=legacy_uuid, pending_source_instance_id=f"legacy-{label}", pending_local_id=1,
                registration_seq=0, immutable_intent_hash="a" * 64, manifest_hash="b" * 64,
                registered_under_mode="required", completeness_generation=1, kind="renew", target_shape="renew_user",
                approval_mode="manual", original_approval_mode="manual", original_approved_by_telegram_id=1000,
                original_approver_evidence_kind="linked_admin", approved_by_telegram_id=1000,
                approver_evidence_kind="linked_admin", auto_approve_policy_snapshot="{}", tenant_scope_key="shared",
                telegram_id_snapshot=7108, telegram_id_source="user_row", target_user_id_snapshot=legacy.id,
                target_purchase_count_snapshot=0, target_username_snapshot="legacyu", amount_snapshot=800,
                state="completed", completed_at=dt.datetime.utcnow(), created_at=dt.datetime.utcnow(), version=0))
            sale = models.LedgerEntry(kind="sale_renew", amount=800, user_id=legacy.id, username_snapshot="legacyu",
                                      approval_uuid=legacy_uuid)
            db.add(sale)
            db.flush()
            db.execute(rv.receipt_approval_effects.insert().values(
                approval_uuid=legacy_uuid, effect_type="ledger_sale", effect_key="sale", resource_type="LedgerEntry",
                resource_id=sale.id, actual_projection="{}", resource_snapshot="{}", delta_value=800,
                created_at=dt.datetime.utcnow()))
            db.commit()
            sale_id = sale.id
        shown = env.preview(legacy_uuid)
        with env.Session() as db:
            voided_sale = db.get(models.LedgerEntry, sale_id).voided_at is not None
        # owner decision 2026-10-20: a renewal void never deletes the account;
        # an account-level renewal has no service to take it back from
        check(L + "renew_user (legacy, no Purchase): refused, the account kept, the sale not voided",
              (shown, env.user_id("legacyu") is not None, voided_sale),
              ((409, "renewal_void_user_level_unsupported"), True, False))

        with env.Session() as db:
            db.query(models.PanelSettings).update({"payment_card_mode": "threshold", "payment_card_switch_threshold": 1500,
                                                    "active_payment_card_id": env.ids.cards[1]})
            db.commit()
        with env.Session() as db:
            from app.services import wallet_accounts
            wallet_accounts.create_user_with_wallet(db, username="cardu", telegram_id=7109)
            db.commit()

        def card_state():
            with env.Session() as db:
                return ([int(db.get(models.PaymentCard, c).accumulated_amount or 0) for c in env.ids.cards],
                        db.get(models.PanelSettings, 1).active_payment_card_id)

        start = card_state()
        y1 = env.purchase("cardu", 7109, payment_card_id=env.ids.cards[1])
        y2 = env.purchase("cardu", 7109, payment_card_id=env.ids.cards[1])
        with env.Session() as db:
            reset = db.execute(select(rv.payment_card_pool_events.c.reset_after).where(
                rv.payment_card_pool_events.c.approval_uuid == y2["approval_uuid"])).scalar()
        rotated = card_state()
        check(L + "threshold pool: the 2nd payment resets card 2 and rotates to card 1",
              (bool(reset), rotated[0][1], rotated[1]), (True, 0, env.ids.cards[0]))
        env.void(env.preview(y2["approval_uuid"])["preview_token"], "cardu")
        after = card_state()
        check(L + "void of the resetting payment: card 2 recomputed from its live events (1000), pointer untouched",
              (after[0][1] - start[0][1], after[1]), (1000, env.ids.cards[0]))
        env.void(env.preview(y1["approval_uuid"])["preview_token"], "cardu")
        check(L + "void of the other one too: card 2 back to its start", card_state()[0][1], start[0][1])

        with env.Session() as db:
            check(L + "the reconciliation finds nothing wrong at the end", wallet_reconcile.check(db)["problems"], [])
            check(L + "every receipt-void flag is still what this test set (no hidden activation)",
                  flags.state(db), {"preview": True, "execute": True, "worker": True})


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="void_hardening_"))
    sqlite = fixture.sqlite_engine(os.path.join(folder, "t.db"))
    cleanup.callback(sqlite.dispose)
    scenario("sqlite", sqlite)
    url = os.environ.get("MARIADB_TEST_URL", "").strip()
    if url:
        maria = scratch.claim(url)
        cleanup.callback(scratch.release, maria)
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
