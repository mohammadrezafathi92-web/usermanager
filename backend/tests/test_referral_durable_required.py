"""A new customer's purchase with an invitation code, durable create_user,
registration 'required' (receipt #1099).

The referrer had two active services and the panel gave no credit, only
quota: the referrer earns nothing (no single service to take the quota),
the new customer still earns 5 GB. The manifest froze the new customer's
reward but named no referrer, and provisioning_approval_exec.build refused
it before T1 with 409 referral_referrer_not_frozen - an approval that stays
'registered' and fails on every retry. The manifest now freezes the
referrer's identity on its own (a zero, optional credit row that is never
written), so the purchase goes through and links exactly that referrer.

Covered: the ambiguous referrer (the incident), a referrer with one service,
credit rewards, an unknown code, the customer's own code, a code that moved
to someone else after registration, a fault inside the reward step and a
retry of the same approval - each reward recorded at most once, on the right
Purchase, and no remote write repeated.

Run:  python3 backend/tests/test_referral_durable_required.py
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-referral-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import func, select

from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import provisioning_parent_execute as execute, quota_rewards, remote_action as action
from app.services import receipt_approval_intent as ri
import _receipt_void_env as fixture

GB = 1024 ** 3
failures: list[str] = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        writes = []

        def counting(dto):
            if dto.action_type is action.ActionType.WG_ENSURE_PRESENT:
                writes.append((dto.identity or {}).get("wg_peer_name"))
            return fixture.runner(dto)
        stack.enter_context(patch.object(execute.remote_runner, "run_action", side_effect=counting))

        def settings(**values):
            with env.Session() as db:
                db.query(models.PanelSettings).update(values)
                db.commit()

        def referrer(name, telegram_id, code, services):
            with env.Session() as db:
                from app.services import wallet_accounts
                user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=telegram_id)
                user.referral_code = code
                db.flush()
                for n in range(services):
                    db.add(models.Purchase(user_id=user.id, comment=f"{name}-{n}", quota_bytes=10 * GB,
                                           status=models.UserStatus.active,
                                           expire_at=dt.datetime.utcnow() + dt.timedelta(days=10)))
                db.commit()
                return user.id

        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="required", loyalty_timing_mode="payment_event", completeness_generation=1))
            db.commit()
        settings(referral_referrer_reward_credit=0, referral_new_user_reward_credit=0,
                 referral_referrer_reward_gb=5.0, referral_new_user_reward_gb=5.0)

        def register(name, telegram_id, code):
            return env.begin("new", name, 1000, package_id=env.ids.shared, claimed_telegram_id=telegram_id,
                             referral_code=code)

        def execute_it(item, name, telegram_id, code):
            return env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
                username=name, telegram_id=telegram_id, package_id=env.ids.shared, paid_amount=1000,
                payment_method="card", approval_uuid=item["approval_uuid"], execution_token=item["execution_token"],
                list_price=1000, referral_code=code), db=db, principal=env.internal))

        def manifest(item):
            with env.Session() as db:
                return {(r["effect_key"]): (r["requirement"], json.loads(r["expected"])) for r in db.execute(
                    select(rv.receipt_approval_expected_effects).where(
                        rv.receipt_approval_expected_effects.c.approval_uuid == item["approval_uuid"])).mappings()
                    if "referral" in r["effect_key"]}

        def outcome(item, name):
            with env.Session() as db:
                user = db.query(models.User).filter_by(username=name).one_or_none()
                purchases = db.query(models.Purchase).filter_by(user_id=user.id).all() if user else []
                recorded = [(r["effect_key"], r["resource_type"], r["resource_id"], r["delta_value"]) for r in
                            db.execute(select(rv.receipt_approval_effects).where(
                                rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"])).mappings()
                            if "referral" in r["effect_key"]]
                return dict(state=env.approval(item["approval_uuid"])["state"],
                            users=db.query(func.count(models.User.id)).filter_by(username=name).scalar(),
                            referred_by=user.referred_by_id if user else None,
                            granted=bool(user.referral_reward_granted) if user else False,
                            balance=int(user.balance or 0) if user else None,
                            purchases=[(p.id, p.quota_bytes) for p in purchases], rewards=recorded)

        def services_of(user_id):
            with env.Session() as db:
                user = db.get(models.User, user_id)
                return int(user.balance or 0), sorted(p.quota_bytes for p in
                                                       db.query(models.Purchase).filter_by(user_id=user_id))

        # ============================================ the incident: two active services, no credit
        amb = referrer("ref-two", 8101, "TWOSVC", services=2)
        item = register("buyer-a", 8201, "TWOSVC")
        rows = manifest(item)
        check(L + "manifest: the new customer's 5 GB reward, and the referrer's identity frozen on its own",
              rows, {"quota:referral:new_user": ("required", {"target": {"ref": "effect:purchase_created:purchase"},
                                                             "bytes": 5 * GB}),
                     "credit:referral_reward:referrer": ("optional", {"referrer_user_id": amb,
                                                                      "referrer_username": "ref-two", "amount": 0})})
        writes.clear()
        result = execute_it(item, "buyer-a", 8201, "TWOSVC")
        check(L + "the approval executes - no referral_referrer_not_frozen", isinstance(result, str), False)
        got = outcome(item, "buyer-a")
        check(L + "completed, one customer, linked to the code's referrer",
              (got["state"], got["users"], got["referred_by"], got["granted"]), ("completed", 1, amb, True))
        check(L + "one Purchase, with the package's 10 GB + the 5 GB reward once",
              [q for _i, q in got["purchases"]], [15 * GB])
        check(L + "exactly one reward recorded, on that Purchase; the zero identity row is never written",
              got["rewards"], [("quota:referral:new_user", "Purchase", got["purchases"][0][0], 5 * GB)])
        check(L + "the referrer, with no single service to reward, gets nothing (as legacy apply_referral_code)",
              services_of(amb), (0, [10 * GB, 10 * GB]))
        check(L + "one remote write for the one connection", len(writes), 1)

        writes.clear()
        again = execute_it(item, "buyer-a", 8201, "TWOSVC")
        got = outcome(item, "buyer-a")
        check(L + "the same approval again: no second customer, Purchase, reward or remote write",
              (got["users"], [q for _i, q in got["purchases"]], len(got["rewards"]), len(writes)),
              (1, [15 * GB], 1, 0))
        check(L + "...the retry answers with the same customer (a replay, not a second run)",
              getattr(again, "username", again), "buyer-a")

        # ============================================ a referrer with one service: unchanged path
        one = referrer("ref-one", 8102, "ONESVC", services=1)
        item = register("buyer-b", 8202, "ONESVC")
        rows = manifest(item)
        check(L + "one service: the referrer's quota row (required) names it; no identity-only row",
              sorted((k, r) for k, (r, _e) in rows.items()),
              [("quota:referral:new_user", "required"), ("quota:referral:referrer", "required")])
        execute_it(item, "buyer-b", 8202, "ONESVC")
        got = outcome(item, "buyer-b")
        check(L + "both rewards once: the referrer's service +5 GB, the new Purchase 15 GB",
              (got["state"], services_of(one), [q for _i, q in got["purchases"]], len(got["rewards"])),
              ("completed", (0, [15 * GB]), [15 * GB], 2))

        # ============================================ credit rewards (the referrer still ambiguous for quota)
        settings(referral_referrer_reward_credit=3000, referral_new_user_reward_credit=1000,
                 referral_referrer_reward_gb=0, referral_new_user_reward_gb=0)
        cred = referrer("ref-credit", 8103, "CREDIT", services=2)
        item = register("buyer-c", 8203, "CREDIT")
        check(L + "credit: the referrer's credit row is the required one (amount 3000), not an identity row",
              manifest(item)["credit:referral_reward:referrer"],
              ("required", {"referrer_user_id": cred, "referrer_username": "ref-credit", "amount": 3000}))
        execute_it(item, "buyer-c", 8203, "CREDIT")
        got = outcome(item, "buyer-c")
        check(L + "credits once each: referrer 3000, new customer 1000",
              (got["state"], services_of(cred)[0], got["balance"], len(got["rewards"])), ("completed", 3000, 1000, 2))
        settings(referral_referrer_reward_credit=0, referral_new_user_reward_credit=0,
                 referral_referrer_reward_gb=5.0, referral_new_user_reward_gb=5.0)

        # ============================================ an unknown code: the sale goes on without a referral
        item = register("buyer-d", 8204, "NOSUCH")
        check(L + "unknown code: no referral row at all", manifest(item), {})
        execute_it(item, "buyer-d", 8204, "NOSUCH")
        got = outcome(item, "buyer-d")
        check(L + "...completed, not linked, no reward, the package's 10 GB",
              (got["state"], got["referred_by"], got["rewards"], [q for _i, q in got["purchases"]]),
              ("completed", None, [], [10 * GB]))

        # ============================================ the customer's own code (same Telegram id)
        referrer("ref-self", 8205, "SELFIE", services=2)
        item = register("buyer-e", 8205, "SELFIE")
        check(L + "own code: no referral row, no identity row", manifest(item), {})
        result = execute_it(item, "buyer-e", 8205, "SELFIE")
        got = outcome(item, "buyer-e")
        check(L + "own code: the purchase itself is not broken", (got["state"], [q for _i, q in got["purchases"]]),
              ("completed", [10 * GB]))
        check(L + "...never a reward to oneself", (got["referred_by"], got["rewards"]), (None, []))

        # ============================================ the code moves to someone else after registration
        moved_from = referrer("ref-moved", 8106, "MOVED", services=2)
        item = register("buyer-f", 8206, "MOVED")
        other = referrer("ref-new-owner", 8107, None, services=2)
        with env.Session() as db:
            db.get(models.User, moved_from).referral_code = None
            db.get(models.User, other).referral_code = "MOVED"
            db.commit()
        result = execute_it(item, "buyer-f", 8206, "MOVED")
        got = outcome(item, "buyer-f")
        check(L + "moved code: refused before any write - the live manifest no longer matches the frozen one",
              (result, got["users"], got["purchases"]), ("manifest_precondition_failed", 0, []))
        check(L + "moved code: nobody is linked or rewarded - not the new owner, not the frozen one",
              (got["referred_by"], got["rewards"], services_of(other), services_of(moved_from)),
              (None, [], (0, [10 * GB, 10 * GB]), (0, [10 * GB, 10 * GB])))
        check(L + "...and the approval does not complete", got["state"] != "completed", True)

        # ============================================ a fault inside the reward step, then a retry
        referrer("ref-fault", 8108, "FAULT", services=2)
        item = register("buyer-g", 8208, "FAULT")
        real_grant, raised = quota_rewards.grant, []

        def failing_grant(resource, amount):
            if not raised:
                raised.append(True)
                raise RuntimeError("injected fault in the reward step")
            return real_grant(resource, amount)
        writes.clear()
        with patch.object(quota_rewards, "grant", failing_grant):
            try:
                first = execute_it(item, "buyer-g", 8208, "FAULT")
            except RuntimeError as exc:
                first = str(exc)
        mid = outcome(item, "buyer-g")
        check(L + "fault: the request fails with that error, the approval stays resumable (not completed)",
              (first, mid["state"] != "completed"), ("injected fault in the reward step", True))
        check(L + "fault: the injected error really fired", raised, [True])
        check(L + "...nothing of the reward step is kept (rolled back with its transaction)",
              (mid["rewards"], mid["granted"], [q for _i, q in mid["purchases"]] in ([], [10 * GB])), ([], False, True))
        first_writes = len(writes)
        execute_it(item, "buyer-g", 8208, "FAULT")
        got = outcome(item, "buyer-g")
        check(L + "the retry completes it: one customer, one Purchase of 15 GB, one reward",
              (got["state"], got["users"], [q for _i, q in got["purchases"]], len(got["rewards"])),
              ("completed", 1, [15 * GB], 1))
        check(L + "the connection was written remotely once in all (not again on the retry)",
              (first_writes, len(writes)), (1, 1))

        # ============================================ hashes of every other manifest are unchanged
        with env.Session() as db:
            target = ri.Target(shape="new_user", owner_admin_id=None, tenant_scope_key="shared", telegram_id=8301,
                               telegram_id_source="bot_attested")
            package = db.get(models.Package, env.ids.shared)
            intent = ri.ApprovalIntent(pending_source_instance_id="h", pending_local_id=1, kind="new",
                                       target_username="h", amount=1000, package_id=env.ids.shared,
                                       referral_code="ONESVC")
            keys = [r.effect_key for r in ri._referral_rows(db, intent, target, package, True, 10 * GB)]
        check(L + "a manifest that already names the referrer gets no extra row (its hash is as before)",
              sorted(keys), ["quota:referral:new_user", "quota:referral:referrer"])


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="referral_durable_"))
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
print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
