"""A legacy bot sale of a brand-new customer records its purchase_created
effect, so its receipt approval (registration 'shadow') completes instead of
staying 'mutating' on missing_effects. SQLite AND real MariaDB (CI). Fake
remote; no network.

Found 2026-10-18 (approvals #147, #156, #168): provision_connection puts every
new connection on a Purchase itself (user_ops._commit_new_connection), so the
bot's absorb_legacy_pool_into_purchase had nothing to move and returned None:
the sale's Purchase was never recorded, never bound for refunds, never given
the package cap, and a referral reward targeting it could not resolve
(effect_manifest_mismatch). The connections here go through the real
_commit_new_connection; only the remote call is faked.

  plain sale       purchase_created recorded on THE sale's Purchase; the
                   approval completes; the Purchase has the package cap
  referral sale    the new customer's quota reward resolves against that
                   Purchase; the approval completes, no shadow error
"""
import contextlib
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-legacy-create-effects-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import select

from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import receipt_approval_registration as registration, user_ops, wallet_accounts
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def fake_provision(db, user, node, protocol, flow="", *_args, **kwargs):
    """The real persistence (_commit_new_connection: the connection on its
    Purchase); only the remote peer/client is not created."""
    connection = models.Connection(user_id=user.id, node_id=node.id, type=protocol, enabled=True,
                                   purchase_batch=kwargs.get("purchase_batch"), xr_flow=flow or None,
                                   package_name_snapshot=kwargs.get("package_name"),
                                   wg_peer_name=f"fx-{user.username}-{node.id}-{getattr(protocol, 'value', protocol)}",
                                   max_concurrent_sessions=1)
    return user_ops._commit_new_connection(db, user, connection)


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(   # production's receipt config
                registration_mode="shadow", loyalty_timing_mode="legacy_activation", completeness_generation=0))
            db.execute(mp.ProvisioningTypeMode.__table__.update().values(mode="legacy"))
            package = db.get(models.Package, env.ids.shared)
            cap = package.max_concurrent_sessions
            sale = dict(quota_gb=package.quota_gb, expire_days=package.duration_days, package_name=package.name)
            specs = [schemas.BotCreateConnectionSpec(node_id=pc.node_id, protocol=pc.protocol.value, flow=pc.flow or None)
                     for pc in package.connections]
            db.commit()

        def sell(username, telegram_id, **extra):
            item = env.begin("new", username, 1000, package_id=env.ids.shared, claimed_telegram_id=telegram_id, **extra)
            with patch.object(user_ops, "provision_connection", side_effect=fake_provision):
                answer = env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
                    username=username, telegram_id=telegram_id, package_id=env.ids.shared, connections=specs, **sale,
                    paid_amount=1000, payment_method="card", approval_uuid=item["approval_uuid"],
                    execution_token=item["execution_token"], list_price=1000), db=db, principal=env.internal))
            return item, answer

        def finalize(item):
            with env.Session() as db:
                result = registration.finalize(db, env.internal, item["approval_uuid"])
                db.commit()
            return result

        def facts(item, username):
            with env.Session() as db:
                user = db.query(models.User).filter_by(username=username).one()
                purchases = db.query(models.Purchase).filter_by(user_id=user.id).all()
                effects = db.execute(select(rv.receipt_approval_effects.c.effect_type, rv.receipt_approval_effects.c.resource_id)
                                     .where(rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"])).all()
                errors = [r[0] for r in db.execute(select(rv.receipt_approval_shadow_events.c.error_code).where(
                    rv.receipt_approval_shadow_events.c.approval_uuid == item["approval_uuid"]))]
            created = [rid for kind, rid in effects if kind == "purchase_created"]
            return purchases, created, errors

        # ------------------------------------------------------------ plain sale
        item, answer = sell("fxplain", 7701)
        result = finalize(item)
        purchases, created, errors = facts(item, "fxplain")
        check(L + "the sale's connections are on ONE Purchase (the real persistence path)", len(purchases), 1)
        check(L + "purchase_created recorded on that Purchase", created, [purchases[0].id])
        check(L + "the approval completes - no missing effects, no shadow error",
              (result["state"], result["missing_effects"], errors), ("completed", [], []))
        check(L + "the sale's Purchase has the package cap", purchases[0].max_concurrent_sessions, cap)

        # ------------------------------------------------------------ referral sale
        with env.Session() as db:
            referrer = wallet_accounts.create_user_with_wallet(db, username="fxref", telegram_id=7702)
            referrer.referral_code = "FXREF"
            db.query(models.PanelSettings).update({"referral_new_user_reward_gb": 5, "referral_referrer_reward_gb": 0,
                                                    "referral_referrer_reward_credit": 0,
                                                    "referral_new_user_reward_credit": 0})
            db.commit()
        item, answer = sell("fxnew", 7703, referral_code="FXREF")
        with env.Session() as db:
            before = db.query(models.Purchase).join(models.User).filter(models.User.username == "fxnew").one().quota_bytes
        env.run(lambda db: bot_router.apply_referral(schemas.ReferralApplyRequest(
            username="fxnew", referral_code="FXREF", approval_uuid=item["approval_uuid"],
            execution_token=item["execution_token"]), db=db, principal=env.internal))
        result = finalize(item)
        purchases, created, errors = facts(item, "fxnew")
        with env.Session() as db:
            reward = db.execute(select(rv.receipt_approval_effects.c.resource_id).where(
                rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"],
                rv.receipt_approval_effects.c.effect_type == "quota_reward_granted")).scalars().all()
        check(L + "the new customer's referral reward lands on the sale's Purchase and is recorded against it",
              (purchases[0].quota_bytes - before, reward), (5 * 1024 ** 3, [purchases[0].id]))
        check(L + "the referral approval completes - no effect_manifest_mismatch, no missing effects",
              (result["state"], result["missing_effects"], errors), ("completed", [], []))


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="legacy_create_effects_"))
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
