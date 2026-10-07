"""Receipt Void batch A1 - referral rewards in the approval manifest and as
recorded effects, the quota-reward target resolver, and the frozen
design's rule for loyalty before phase P9a (design 5.3, 14.1, 14.2).

Run:  python3 backend/tests/test_receipt_approval_referral_rewards.py

Runs on SQLite always and, through tests/_mariadb_scratch.py, on a real
MariaDB when MARIADB_TEST_URL is set (mandatory in CI).

P5 now applies quota rewards to the manifest destination: one finite User
or Purchase, never an unlimited or ambiguous resource. Credits and legacy
counting timing remain independent of that quota destination.
"""
from __future__ import annotations

import inspect
import os
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import _mariadb_scratch as scratch
import _no_network

# Nothing in this test may reach a node: every outgoing connection is refused
# and recorded, except to the MariaDB test server.
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.routers import receipt_approvals as approvals_router
from app.services import bot_auth, receipt_void_schema, user_ops
from app.services import receipt_approval_effects as fx
from app.services import receipt_approval_intent as ri
from app.services import receipt_approval_registration as reg
from app.services import receipt_approval_runtime as runtime
from app.telegram_bot.handlers import admin_pending

failures: list[str] = []
GB = 1024 ** 3
F, SH, A = rv.receipt_approval_effects, rv.receipt_approval_shadow_events, rv.receipt_approvals


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


print("--- the resolver (design 14.1), verbatim ---")
P = lambda quota: SimpleNamespace(quota_bytes=quota)
one, unlimited = P(5 * GB), P(0)
check("no purchase: the User if its quota is finite, else nothing (unlimited)",
      (ri.resolve_quota_reward_target([], 10), ri.resolve_quota_reward_target([], 0), ri.resolve_quota_reward_target([], None)),
      (("User", None), None, None))
check("exactly one purchase: that purchase if finite, else nothing - never falls back to the User",
      (ri.resolve_quota_reward_target([one], 0), ri.resolve_quota_reward_target([unlimited], 10 * GB)), (("Purchase", one), None))
check("more than one purchase: ambiguous, nothing - even if all but one are unlimited",
      (ri.resolve_quota_reward_target([one, P(GB)], 10), ri.resolve_quota_reward_target([one, unlimited], 10)), (None, None))
check("the resolver is pure: no database, no session", "db" in inspect.signature(ri.resolve_quota_reward_target).parameters, False)

real_provision = user_ops.provision_connection
# create_user's response asks the NODE for a WireGuard connection's config
# (user_ops.get_connection_share -> MikroTik). Not in a test.
user_ops.get_connection_share = lambda connection: {}


def fake_provision(db_, user, node_, protocol, flow="", *args, **kwargs):
    """Stands in for the remote call: only the Connection row."""
    batch = kwargs.get("purchase_batch") or next((a for a in args if isinstance(a, str) and len(a) == 32), None)
    connection = models.Connection(user_id=user.id, node_id=node_.id, type=models.ConnectionType(protocol), purchase_batch=batch)
    db_.add(connection)
    db_.flush()
    return connection


def scenario(label: str, engine) -> None:
    L = f"[{label}] "
    models.Base.metadata.create_all(engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)      # the application's settings
    check(L + "schema ready", receipt_void_schema.bootstrap(engine, Session)["ready"], True)
    db = Session()
    internal = bot_auth.BotPrincipal.internal(None)
    settings_row = models.PanelSettings(id=1, referral_referrer_reward_credit=3000, referral_referrer_reward_gb=2,
                                        referral_new_user_reward_credit=1000, referral_new_user_reward_gb=1,
                                        loyalty_purchase_threshold=100, loyalty_reward_credit=500, loyalty_reward_gb=1)
    node = models.Node(name="n1", type=models.NodeType.mikrotik)
    db.add_all([settings_row, node, models.AdminUser(username="root", hashed_password="x", is_superadmin=True, telegram_id=1000)])
    db.flush()
    plain = models.Package(name="plain", quota_gb=10, duration_days=30, price=1000)          # no bundled connection
    bundle = models.Package(name="bundle", quota_gb=10, duration_days=30, price=1000)
    unlimited_pkg = models.Package(name="unlimited", quota_gb=0, duration_days=30, price=1000)
    db.add_all([plain, bundle, unlimited_pkg])
    db.flush()
    db.add(models.PackageConnection(package_id=bundle.id, node_id=node.id, protocol=models.ConnectionType.wireguard))
    # referrers, each with a different topology
    r_user = models.User(username="ref_user_level", telegram_id=1, referral_code="RUSER", total_quota_bytes=5 * GB, balance=100)
    r_one = models.User(username="ref_one_purchase", telegram_id=2, referral_code="RONE", total_quota_bytes=0)
    r_two = models.User(username="ref_two_purchases", telegram_id=3, referral_code="RTWO", total_quota_bytes=7 * GB)
    r_unlimited = models.User(username="ref_unlimited", telegram_id=4, referral_code="RUNL", total_quota_bytes=0)
    db.add_all([r_user, r_one, r_two, r_unlimited])
    db.flush()
    one_purchase = models.Purchase(user_id=r_one.id, package_id=bundle.id, quota_bytes=4 * GB)
    db.add_all([one_purchase, models.Purchase(user_id=r_two.id, package_id=bundle.id, quota_bytes=GB),
                models.Purchase(user_id=r_two.id, package_id=bundle.id, quota_bytes=GB)])
    db.commit()
    ids = SimpleNamespace(plain=plain.id, bundle=bundle.id, unlimited=unlimited_pkg.id, node=node.id, r_user=r_user.id,
                          r_one=r_one.id, r_two=r_two.id, r_unlimited=r_unlimited.id, one_purchase=one_purchase.id)
    runtime.set_requested_mode(db, registration_mode="shadow")
    db.commit()
    counter = [0]

    def intent(username, package_id, code, telegram_id):
        counter[0] += 1
        return ri.ApprovalIntent(pending_source_instance_id=f"inst-{label}", pending_local_id=counter[0], kind="new",
                                 target_username=username, amount=900, package_id=package_id, list_price=1000,
                                 referral_code=code, claimed_telegram_id=telegram_id)

    def rows_of(the_intent):
        target = ri.resolve_target(db, internal, the_intent)
        return {f"{r.effect_type}:{r.effect_key}": r for r in ri.build_manifest(db, the_intent, target)}

    def reward_keys(the_intent):
        return sorted(k for k in rows_of(the_intent) if "referral" in k)

    def effects(uuid_):
        db.expire_all()
        return sorted(f"{r.effect_type}:{r.effect_key}" for r in db.execute(select(F).where(F.c.approval_uuid == uuid_)))

    def shadow_codes(uuid_):
        return sorted(r.error_code for r in db.execute(select(SH).where(SH.c.approval_uuid == uuid_)))

    def user(name):
        db.expire_all()
        return db.query(models.User).filter_by(username=name).one()

    def sell(username, package_id, code, telegram_id, connections=()):
        """The bot's own sequence for a brand-new customer: register, create the user, apply the referral, finalize."""
        the_intent = intent(username, package_id, code, telegram_id)
        answer = reg.begin(db, internal, the_intent, approval_mode="manual", approved_by_telegram_id=1000)
        uuid_ = answer["approval_uuid"]
        bot_router.create_user(schemas.BotCreateUserRequest(
            username=username, quota_gb=10 if package_id != ids.unlimited else 0, expire_days=30, telegram_id=telegram_id,
            connections=list(connections), package_id=package_id, paid_amount=900, payment_method="card",
            approval_uuid=uuid_), db=db, principal=internal)
        result = bot_router.apply_referral(schemas.ReferralApplyRequest(username=username, referral_code=code,
                                                                        approval_uuid=uuid_), db=db, principal=internal)
        final = approvals_router.finalize_approval(db, internal, uuid_)
        return uuid_, result, final

    spec = [schemas.BotCreateConnectionSpec(node_id=ids.node, protocol="wireguard", flow="")]
    ALL_FOUR = ["quota_reward_granted:quota:referral:new_user", "quota_reward_granted:quota:referral:referrer",
                "wallet_credit_source_created:credit:referral_reward:new_user",
                "wallet_credit_source_created:credit:referral_reward:referrer"]

    print(f"--- {label}: the manifest ---")
    base = intent("m1", ids.plain, "ruser", 501)
    m = rows_of(base)
    check(L + "a valid code adds four REQUIRED rows; the code's case does not matter",
          (reward_keys(base), {m[k].requirement for k in ALL_FOUR}), (ALL_FOUR, {"required"}))
    check(L + "the credits: amounts frozen from the settings; the referrer by id and name, the new customer by reference",
          (m[ALL_FOUR[3]].expected, m[ALL_FOUR[2]].expected),
          ({"referrer_user_id": ids.r_user, "referrer_username": "ref_user_level", "amount": 3000},
           {"user": ri.USER_REF, "amount": 1000}))
    check(L + "referrer with no purchase and a finite quota: the User is the target",
          m[ALL_FOUR[1]].expected, {"target": {"type": "User", "id": ids.r_user}, "bytes": 2 * GB})
    check(L + "new customer WITHOUT a connection: the reward targets the user this approval creates",
          m[ALL_FOUR[0]].expected, {"target": ri.USER_REF, "bytes": GB})
    check(L + "new customer WITH a connection: the one Purchase this approval creates",
          rows_of(intent("m2", ids.bundle, "RUSER", 502))[ALL_FOUR[0]].expected, {"target": ri.PURCHASE_REF, "bytes": GB})
    check(L + "referrer with exactly one finite Purchase: that Purchase, not the User",
          rows_of(intent("m3", ids.plain, "RONE", 503))[ALL_FOUR[1]].expected,
          {"target": {"type": "Purchase", "id": ids.one_purchase}, "bytes": 2 * GB})
    check(L + "referrer with two purchases (ambiguous) or unlimited: NO quota row for them - the credit row stays",
          (reward_keys(intent("m4", ids.plain, "RTWO", 504)), reward_keys(intent("m5", ids.plain, "RUNL", 505))),
          ([k for k in ALL_FOUR if k != ALL_FOUR[1]],) * 2)
    check(L + "an unlimited package: no quota row for the new customer",
          reward_keys(intent("m6", ids.unlimited, "RUSER", 506)), [k for k in ALL_FOUR if k != ALL_FOUR[0]])
    check(L + "an unknown code, or no code: no referral row at all - and the approval is still buildable",
          (reward_keys(intent("m7", ids.plain, "NOPE", 507)), reward_keys(intent("m8", ids.plain, None, 508)),
           "user_created:user" in rows_of(intent("m9", ids.plain, "NOPE", 509))), ([], [], True))
    existing = ri.ApprovalIntent(pending_source_instance_id="x", pending_local_id=9999, kind="new",
                                 target_username="ref_user_level", amount=1, package_id=ids.plain, referral_code="RONE")
    check(L + "referral is only for a brand-new customer: an existing one buying again gets no referral row",
          reward_keys(existing), [])
    for field in ("referral_referrer_reward_credit", "referral_referrer_reward_gb", "referral_new_user_reward_credit",
                  "referral_new_user_reward_gb"):
        setattr(db.get(models.PanelSettings, 1), field, 0)
    db.commit()
    check(L + "every reward set to zero: no row for a zero-value reward", reward_keys(intent("m10", ids.plain, "RUSER", 510)), [])
    row = db.get(models.PanelSettings, 1)
    row.referral_referrer_reward_credit, row.referral_referrer_reward_gb = 3000, 2
    row.referral_new_user_reward_credit, row.referral_new_user_reward_gb = 1000, 1
    db.commit()
    check(L + "loyalty before phase P9a: NO loyalty row in any manifest, although loyalty is switched on (design 14.2, RV-344)",
          ([k for k in rows_of(intent("m11", ids.plain, "RUSER", 511)) if "loyalty" in k],
           runtime.read_state(db)["loyalty_timing_mode"]), ([], "legacy_activation"))

    print(f"--- {label}: effects, recorded from what really happened ---")
    before = (user("ref_user_level").balance, user("ref_user_level").total_quota_bytes)
    uuid_a, result, final = sell("buyer_a", ids.plain, "RUSER", 601)
    check(L + "user-level referrer, new customer without a connection: all four rewards recorded and the approval completes",
          (result, [k for k in effects(uuid_a) if "referral" in k], shadow_codes(uuid_a), final["state"], final["missing_effects"]),
          ({"ok": True, "reason": ""}, ALL_FOUR, [], "completed", []))
    referrer, buyer = user("ref_user_level"), user("buyer_a")
    check(L + "the rewards themselves are exactly what the panel always gave: referrer +3000 and +2GB",
          (referrer.balance - before[0], referrer.total_quota_bytes - before[1], buyer.referred_by_id), (3000, 2 * GB, ids.r_user))
    quota_effect = dict(db.execute(select(F).where(F.c.approval_uuid == uuid_a,
                                                   F.c.effect_key == "quota:referral:referrer")).mappings().one())
    import json
    snapshot = json.loads(quota_effect["resource_snapshot"])
    check(L + "the quota effect names the resource really written, the bytes, and before/after for a later reversal",
          (quota_effect["resource_type"], quota_effect["resource_id"], quota_effect["delta_value"], snapshot["quota_before"],
           snapshot["quota_after"], snapshot["before_was_unlimited"]),
          ("User", ids.r_user, 2 * GB, before[1], before[1] + 2 * GB, False))

    print(f"--- {label}: the reward and its record are ONE transaction ---")
    from sqlalchemy import event as sa_event
    seen = {}

    def at_commit(session):
        if session.in_nested_transaction():
            return                       # a savepoint being released, not the transaction's commit
        # what this very transaction holds at the moment it is about to commit
        seen["effects"] = sorted(k for k in (f"{r.effect_type}:{r.effect_key}" for r in session.execute(
            select(F).where(F.c.approval_uuid == seen["uuid"]))) if "referral" in k)
        seen["referred"] = session.query(models.User).filter_by(username=seen["buyer"]).one().referred_by_id
        if seen.pop("crash", False):
            raise RuntimeError("process died at commit")

    the_intent = intent("buyer_atomic", ids.plain, "RUSER", 650)
    uuid_x = reg.begin(db, internal, the_intent, approval_mode="manual", approved_by_telegram_id=1000)["approval_uuid"]
    bot_router.create_user(schemas.BotCreateUserRequest(
        username="buyer_atomic", quota_gb=10, expire_days=30, telegram_id=650, package_id=ids.plain, paid_amount=900,
        payment_method="card", approval_uuid=uuid_x), db=db, principal=internal)
    referrer_before = (user("ref_user_level").balance, user("ref_user_level").total_quota_bytes)
    seen.update(uuid=uuid_x, buyer="buyer_atomic", crash=True)
    sa_event.listen(db, "before_commit", at_commit)
    try:
        try:
            bot_router.apply_referral(schemas.ReferralApplyRequest(username="buyer_atomic", referral_code="RUSER",
                                                                   approval_uuid=uuid_x), db=db, principal=internal)
            crashed = "no"
        except RuntimeError:
            crashed = "yes"
            db.rollback()
        check(L + "at the moment of commit the SAME transaction already holds the reward AND all four effects",
              (crashed, seen["effects"], seen["referred"]), ("yes", ALL_FOUR, ids.r_user))
        check(L + "a crash at that commit leaves NOTHING: no reward, no referral link, no effect - so nothing is stuck",
              ((user("ref_user_level").balance, user("ref_user_level").total_quota_bytes) == referrer_before,
               user("buyer_atomic").referred_by_id, user("buyer_atomic").referral_reward_granted,
               [k for k in effects(uuid_x) if "referral" in k]), (True, None, False, []))
        retried = bot_router.apply_referral(schemas.ReferralApplyRequest(username="buyer_atomic", referral_code="RUSER",
                                                                         approval_uuid=uuid_x), db=db, principal=internal)
    finally:
        sa_event.remove(db, "before_commit", at_commit)
    check(L + "the retry then applies the reward once and records it, and the approval completes",
          (retried["ok"], user("ref_user_level").balance - referrer_before[0], [k for k in effects(uuid_x) if "referral" in k],
           approvals_router.finalize_approval(db, internal, uuid_x)["state"]), (True, 3000, ALL_FOUR, "completed"))
    check(L + "the endpoint records through apply_referral_code's before_commit hook, with no commit of its own",
          ("before_commit=" in inspect.getsource(bot_router.apply_referral), "db.commit()" in inspect.getsource(bot_router.apply_referral)),
          (True, False))

    print(f"--- {label}: retry and idempotency ---")
    state_before = (user("ref_user_level").balance, user("ref_user_level").total_quota_bytes, user("buyer_a").balance,
                    len(effects(uuid_a)))
    again = bot_router.apply_referral(schemas.ReferralApplyRequest(username="buyer_a", referral_code="RUSER",
                                                                   approval_uuid=uuid_a), db=db, principal=internal)
    check(L + "applying the referral a second time grants nothing and records nothing",
          (again["ok"], (user("ref_user_level").balance, user("ref_user_level").total_quota_bytes, user("buyer_a").balance,
                         len(effects(uuid_a))) == state_before, shadow_codes(uuid_a)), (False, True, []))

    print(f"--- {label}: actual rewards and manifest have the same destination ---")
    user_ops.provision_connection = fake_provision
    try:
        uuid_b, _result, final_b = sell("buyer_b", ids.bundle, "RONE", 602, spec)
    finally:
        user_ops.provision_connection = real_provision
    check(L + "both quota rewards target the actual Purchases and all four effects are recorded",
          ([k for k in effects(uuid_b) if "referral" in k], shadow_codes(uuid_b).count("effect_manifest_mismatch")),
          (ALL_FOUR, 0))
    check(L + "the approval completes with no missing quota effect",
          (final_b["state"], final_b["missing_effects"]), ("completed", []))
    check(L + "referrer's User quota remains unlimited; its actual Purchase receives the reward",
          (user("buyer_b").referred_by_id, user("ref_one_purchase").total_quota_bytes,
           db.get(models.Purchase, ids.one_purchase).quota_bytes), (ids.r_one, 0, 6 * GB))
    buyer_purchase = db.query(models.Purchase).filter_by(user_id=user("buyer_b").id).one()
    quota_effect = db.execute(select(F).where(F.c.approval_uuid == uuid_b,
                                            F.c.effect_key == "quota:referral:new_user")).mappings().one()
    check(L + "new buyer reward is bound to the created Purchase with exact before/after evidence",
          (buyer_purchase.quota_bytes, quota_effect["resource_type"], quota_effect["resource_id"],
           json.loads(quota_effect["resource_snapshot"])["quota_before"],
           json.loads(quota_effect["resource_snapshot"])["quota_after"]),
          (11 * GB, "Purchase", buyer_purchase.id, 10 * GB, 11 * GB))
    uuid_c, _result, final_c = sell("buyer_c", ids.plain, "RTWO", 603)
    check(L + "ambiguous referrer gets credit, not a guessed quota reward; no mismatch is logged",
          ([k for k in effects(uuid_c) if "referral" in k], shadow_codes(uuid_c), final_c["state"]),
          ([k for k in ALL_FOUR if k != ALL_FOUR[1]], [], "completed"))
    check(L + "ambiguous referrer's dormant User quota is untouched", user("ref_two_purchases").total_quota_bytes, 7 * GB)
    uuid_d, _result, _final = sell("buyer_d", ids.plain, "RUNL", 604)
    check(L + "unlimited stays unlimited and no unwanted effect is attempted",
          (shadow_codes(uuid_d), user("ref_unlimited").total_quota_bytes), ([], 0))
    uuid_e, result_e, final_e = sell("buyer_e", ids.plain, "NOPE", 605)
    check(L + "an invalid code: the sale completes without any referral, as it always did",
          (result_e["ok"], [k for k in effects(uuid_e) if "referral" in k], shadow_codes(uuid_e), final_e["state"],
           user("buyer_e").referred_by_id), (False, [], [], "completed", None))

    print(f"--- {label}: the customer's own code ---")
    own = intent("buyer_self", ids.plain, "RUSER", 1)          # Telegram id 1 is ref_user_level's own id
    check(L + "a referrer with the customer's own Telegram id is not a referral: the manifest has no referral row",
          (reward_keys(own), "user_created:user" in rows_of(own)), ([], True))
    no_tg = models.User(username="ref_no_telegram", referral_code="RNOTG", total_quota_bytes=GB)
    db.add(no_tg)
    db.commit()
    check(L + "a referrer with no Telegram id cannot be 'the same person': their code is an ordinary referral",
          len(reward_keys(intent("buyer_g", ids.plain, "RNOTG", 607))), 4)
    referrer_before = user("ref_user_level").balance
    uuid_s, result_s, final_s = sell("buyer_self", ids.plain, "RUSER", 1)
    check(L + "what the panel does with such a code is NOT changed here (it still pays, as it always has) - but none of it "
              "is recorded as this approval's effect: each reward is refused as not-in-manifest and logged",
          (result_s["ok"], user("ref_user_level").balance - referrer_before, [k for k in effects(uuid_s) if "referral" in k],
           shadow_codes(uuid_s), final_s["state"]),
          (True, 3000, [], ["effect_not_in_manifest"] * 4, "completed"))

    print(f"--- {label}: without an approval nothing at all is different ---")
    bot_router.create_user(schemas.BotCreateUserRequest(username="legacy_buyer", quota_gb=10, expire_days=30, telegram_id=701,
                                                        package_id=ids.plain, paid_amount=900, payment_method="card"),
                           db=db, principal=internal)
    effects_before = len(db.execute(select(F.c.id)).all())
    referrer_before = (user("ref_user_level").balance, user("ref_user_level").total_quota_bytes)
    legacy = bot_router.apply_referral(schemas.ReferralApplyRequest(username="legacy_buyer", referral_code="RUSER"),
                                       db=db, principal=internal)
    buyer = user("legacy_buyer")
    check(L + "same rewards on the same rows, nothing recorded",
          (legacy, user("ref_user_level").balance - referrer_before[0], user("ref_user_level").total_quota_bytes - referrer_before[1],
           buyer.referred_by_id, buyer.referral_reward_granted, len(db.execute(select(F.c.id)).all()) == effects_before),
          ({"ok": True, "reason": ""}, 3000, 2 * GB, ids.r_user, True, True))
    check(L + "the new customer got exactly the referral rewards, on the User row",
          (buyer.balance, buyer.total_quota_bytes), (1000, 10 * GB + GB))

    print(f"--- {label}: a legacy loyalty reward that fires during an approval (before P9a) ---")
    db.get(models.PanelSettings, 1).loyalty_purchase_threshold = 1          # due on the very first purchase
    db.commit()
    uuid_f, result_f, final_f = sell("buyer_f", ids.plain, "RUSER", 606)
    buyer = user("buyer_f")
    check(L + "loyalty works exactly as today: the reward is given (credit 500, 1GB) on top of the referral rewards",
          (result_f["ok"], buyer.balance, buyer.total_quota_bytes, buyer.loyalty_rewards_given),
          (True, 1000 + 500, 10 * GB + GB + GB, 1))
    check(L + "no loyalty effect is recorded - there is nothing in the manifest to record it against",
          [k for k in effects(uuid_f) if "loyalty" in k], [])
    check(L + "sale evidence is captured before the separate legacy loyalty grant; shadow can complete",
          (final_f["state"], "effect_manifest_mismatch" in shadow_codes(uuid_f), "user_created:user" in final_f["missing_effects"]),
          ("completed", False, False))
    user_ops.provision_connection = fake_provision
    try:
        uuid_g, _, final_g = sell("buyer_loyal_purchase", ids.bundle, "NOPE", 608, spec)
    finally:
        user_ops.provision_connection = real_provision
    buyer = user("buyer_loyal_purchase")
    purchase = db.query(models.Purchase).filter_by(user_id=buyer.id).one()
    check(L + "loyalty is applied after absorption to the actual Purchase, once",
          (purchase.quota_bytes, buyer.balance, buyer.loyalty_rewards_given, final_g["state"], shadow_codes(uuid_g)),
          (11 * GB, 500, 1, "completed", []))
    user_ops._maybe_grant_loyalty_reward(db, buyer)
    db.commit()
    check(L + "retry does not duplicate the loyalty reward", (purchase.quota_bytes, buyer.balance), (11 * GB, 500))
    uuid_u, _, _ = sell("buyer_loyal_unlimited", ids.unlimited, "NOPE", 609)
    buyer = user("buyer_loyal_unlimited")
    check(L + "unlimited loyalty destination stays unlimited while credit and progress apply",
          (buyer.total_quota_bytes, buyer.balance, buyer.loyalty_rewards_given), (0, 500, 1))
    # The multi-purchase referrer still gets credit when its loyalty count
    # crosses a threshold, but neither Purchase nor its dormant User is
    # guessed as a quota destination.
    ambiguous = user("ref_two_purchases")
    ambiguous.purchase_count = 1
    before_credit = ambiguous.balance
    before_quotas = sorted(p.quota_bytes for p in db.query(models.Purchase).filter_by(user_id=ambiguous.id))
    user_ops._maybe_grant_loyalty_reward(db, ambiguous)
    db.commit()
    check(L + "ambiguous loyalty retains all quota resources and only grants credit",
          (ambiguous.total_quota_bytes, ambiguous.balance - before_credit,
           sorted(p.quota_bytes for p in db.query(models.Purchase).filter_by(user_id=ambiguous.id))),
          (7 * GB, 500, before_quotas))
    db.get(models.PanelSettings, 1).loyalty_purchase_threshold = 100
    db.commit()
    db.close()
    check(L + "the whole scenario made no attempt to reach a node or any other host", _no_network.attempts, [])


print("--- evidence and projection rules ---")
target_user = SimpleNamespace(id=7, username="u")
good = fx.QuotaRewardEvidence("User", 7, 5, 9, False)


def rejected(*args):
    try:
        fx._project(*args)
    except fx.EffectRejected as exc:
        return exc.code
    return None


check("valid quota evidence projects the target and the granted bytes",
      fx._project("quota_reward_granted", "quota:referral:referrer", target_user, good)[2:],
      ({"target": {"type": "User", "id": 7}, "bytes": 4}, 4))
check("refused: evidence about another resource, quota that did not grow, a wrong before_was_unlimited, no evidence, "
      "the wrong evidence type",
      [rejected("quota_reward_granted", "quota:referral:referrer", target_user, e) for e in (
          fx.QuotaRewardEvidence("User", 8, 5, 9, False), fx.QuotaRewardEvidence("Purchase", 7, 5, 9, False),
          fx.QuotaRewardEvidence("User", 7, 9, 9, False), fx.QuotaRewardEvidence("User", 7, 0, 4, False),
          fx.QuotaRewardEvidence("User", 7, 5, 9, True), None, fx.WalletCreditEvidence(1, 2))],
      ["effect_evidence_invalid"] * 7)
check("a reward onto an unlimited quota is VALID evidence of what happened (it is the manifest that has no row for it)",
      rejected("quota_reward_granted", "quota:referral:new_user", target_user, fx.QuotaRewardEvidence("User", 7, 0, 4, True)), None)
check("credit: refused when the balance did not grow or more than one reward is claimed",
      [rejected("wallet_credit_source_created", "credit:referral_reward:referrer", target_user, e)
       for e in (fx.WalletCreditEvidence(5, 5), fx.WalletCreditEvidence(5, 9, 2), None)], ["effect_evidence_invalid"] * 3)
check("no loyalty effect can be recorded at all before P9a",
      [rejected(t, k, target_user, None) for t, k in (("loyalty_purchase_recorded", "loyalty_purchase"),
                                                       ("wallet_credit_source_created", "credit:loyalty:1:1"),
                                                       ("quota_reward_granted", "quota:loyalty:1:1"))],
      ["effect_not_in_manifest"] * 3)
check("the snapshot of a quota reward keeps before/after and drops nothing sensitive",
      sorted(fx._snapshot("User", 7, {"bytes": 4}, good)), ["before_was_unlimited", "id", "quota_after", "quota_before", "rewards_count"])
check("the evidence is built inside user_ops (the service that did the mutation), the endpoint only passes it on",
      ("QuotaRewardEvidence(" in inspect.getsource(user_ops._referral_evidence),
       "Evidence(" in inspect.getsource(bot_router.apply_referral)), (True, False))
check("the bot passes the approval to apply_referral",
      "approval_uuid=session.get(\"approval_uuid\")" in inspect.getsource(admin_pending.perform_approval), True)

scenario("sqlite", create_engine(f"sqlite:///{tempfile.mkdtemp()}/t.db"))

mariadb_url = os.environ.get("MARIADB_TEST_URL", "").strip()
if mariadb_url:
    try:
        maria = scratch.claim(mariadb_url)
    except scratch.ScratchRefused as refused:
        maria = None
        check(f"MARIADB_TEST_URL must allow creating a run database ({refused})", False)
    if maria is not None:
        try:
            scenario("mariadb", maria)
        finally:
            scratch.release(maria)
elif os.environ.get("CI"):
    check("CI must provide MARIADB_TEST_URL (real MariaDB is mandatory in CI, never skipped)", False)
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")

print()
if failures:
    print(f"{len(failures)} FAILED:")
    for label in failures:
        print(f"  - {label}")
    sys.exit(1)
print("all checks passed")
