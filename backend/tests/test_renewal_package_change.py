"""A paid renewal can change package, without weakening its target fence.

Real bot endpoint, SQLite and MariaDB; all remote actions are fake.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-renew-package-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import select
from app import models, models_provisioning as mp, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import user_ops, wallet_accounts
import _receipt_void_env as fixture

GB = 1024 ** 3


def scenario(label, engine):
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        with env.Session() as db:
            packages = [models.Package(name="Original", quota_gb=0, duration_days=30, price=100,
                                       max_concurrent_sessions=1),
                        models.Package(name="20 GiB, two users", quota_gb=20, duration_days=30, price=180,
                                       max_concurrent_sessions=2),
                        models.Package(name="Unlimited quota", quota_gb=0, duration_days=30, price=200,
                                       max_concurrent_sessions=3),
                        models.Package(name="No expiry", quota_gb=20, duration_days=None, price=250,
                                       max_concurrent_sessions=2),
                        models.Package(name="Unlimited", quota_gb=0, duration_days=None, price=300,
                                       max_concurrent_sessions=2)]
            db.add_all(packages)
            db.flush()
            # SQLAlchemy's insert default maps None to 30 days; an edited
            # package may explicitly have no duration.
            packages[3].duration_days = packages[4].duration_days = None
            db.commit()
            original, finite, unlimited, no_expiry, unbounded = [p.id for p in packages]
        seq = iter(range(8201, 8300))

        def customer(source=original, *, quota=0, usable=False):
            tg = next(seq)
            name = f"renew{tg}"
            with env.Session() as db:
                user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=tg)
                db.flush()
                purchase = models.Purchase(user_id=user.id, package_id=source, quota_bytes=quota,
                    used_bytes=0, max_concurrent_sessions=1,
                    expire_at=dt.datetime.utcnow() + dt.timedelta(days=2 if usable else -1),
                    status=models.UserStatus.active if usable else models.UserStatus.expired)
                db.add(purchase)
                db.commit()
                return name, purchase.id

        def register(name, purchase_id, package):
            with env.Session() as db:
                p = db.get(models.Package, package)
                values = (p.price, p.quota_gb, int(p.duration_days or 0))
            item = env.begin("renew", name, values[0], package_id=package, renew_purchase_id=purchase_id)
            assert item.get("state") == "registered", item
            return item, values

        def execute(name, purchase_id, package, item, values):
            amount, gb, days = values
            return env.run(lambda db: bot_router.renew_service(name, purchase_id, schemas.BotRenewRequest(
                package_id=package, add_gb=gb, add_days=days, paid_amount=amount, list_price=amount,
                payment_method="card", approval_uuid=item["approval_uuid"],
                execution_token=item["execution_token"]), db=db, owner_admin_id=None, principal=env.internal))

        def complete(name, purchase_id, package):
            with env.Session() as db:
                source_package = db.get(models.Purchase, purchase_id).package_id
            item, values = register(name, purchase_id, package)
            before = env.approval(item["approval_uuid"])
            assert before["target_purchase_id_snapshot"] == purchase_id
            assert before["target_purchase_package_id_snapshot"] == source_package
            result = execute(name, purchase_id, package, item, values)
            assert not isinstance(result, str), result
            row = env.approval(item["approval_uuid"])
            assert row["state"] == "completed", row["state"]
            with env.Session() as db:
                operations = db.query(mp.ProvisioningOperation).filter_by(approval_uuid=item["approval_uuid"]).all()
                assert len(operations) == 1 and operations[0].state == "completed"
                actual = db.execute(select(rv.receipt_approval_effects.c.effect_type,
                    rv.receipt_approval_effects.c.effect_key).where(
                    rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"])).all()
                expected = db.execute(select(rv.receipt_approval_expected_effects.c.effect_type,
                    rv.receipt_approval_expected_effects.c.effect_key).where(
                    rv.receipt_approval_expected_effects.c.approval_uuid == item["approval_uuid"])).all()
                assert sorted(actual) == sorted(expected)
                assert db.query(models.LedgerEntry).filter_by(approval_uuid=item["approval_uuid"]).count() == 1
            assert not isinstance(execute(name, purchase_id, package, item, values), str)
            with env.Session() as db:
                assert db.query(mp.ProvisioningOperation).filter_by(approval_uuid=item["approval_uuid"]).count() == 1
                assert db.query(models.LedgerEntry).filter_by(approval_uuid=item["approval_uuid"]).count() == 1
            return item

        # Unlimited -> finite and 1 -> 2 users, immediately on an expired service.
        name, pid = customer()
        complete(name, pid, finite)
        with env.Session() as db:
            p = db.get(models.Purchase, pid)
            assert (p.package_id, p.quota_bytes, p.max_concurrent_sessions, p.used_bytes) == (finite, 20 * GB, 2, 0)
            assert p.expire_at > dt.datetime.utcnow() and p.status == models.UserStatus.active

        # A usable unlimited service queues the finite package, retaining its current limits.
        name, pid = customer(usable=True)
        complete(name, pid, finite)
        with env.Session() as db:
            p = db.get(models.Purchase, pid)
            assert (p.package_id, p.quota_bytes, p.max_concurrent_sessions) == (original, 0, 1)
            assert (p.reserved_package_id, p.reserved_quota_bytes, p.reserved_max_concurrent_sessions) == (finite, 20 * GB, 2)
            p.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
            assert user_ops._maybe_activate_reserved_purchase_renewal(db, p)
            assert (p.package_id, p.quota_bytes, p.max_concurrent_sessions) == (finite, 20 * GB, 2)
            db.commit()

        # Finite -> unlimited quota, both queued and immediate; zero replaces the old quota.
        for usable in (False, True):
            name, pid = customer(finite, quota=20 * GB, usable=usable)
            complete(name, pid, unlimited)
            with env.Session() as db:
                p = db.get(models.Purchase, pid)
                if usable:
                    assert p.quota_bytes == 20 * GB and p.reserved_quota_bytes == 0
                    assert user_ops._maybe_activate_reserved_purchase_renewal(db, p)
                assert (p.quota_bytes, p.package_id, p.max_concurrent_sessions) == (0, unlimited, 3)
                db.commit()

        # No-expiry and fully unlimited packages also replace their zero dimensions.
        for package in (no_expiry, unbounded):
            for usable in (False, True):
                name, pid = customer(finite, quota=20 * GB, usable=usable)
                complete(name, pid, package)
                with env.Session() as db:
                    p = db.get(models.Purchase, pid)
                    if usable:
                        assert p.expire_at is not None and p.reserved_duration_days == 0
                        assert user_ops._maybe_activate_reserved_purchase_renewal(db, p)
                    assert p.expire_at is None and p.package_id == package, (
                        package, usable, p.expire_at, p.reserved_duration_days)
                    assert p.quota_bytes == (0 if package == unbounded else 20 * GB)
                    db.commit()

        # A service without an original package has a captured NULL, not a missing snapshot.
        name, pid = customer(None)
        complete(name, pid, finite)

        # Adding a finite package cannot erase an unlimited queued limit.
        name, pid = customer(finite, quota=20 * GB, usable=True)
        complete(name, pid, unbounded)
        complete(name, pid, finite)
        with env.Session() as db:
            p = db.get(models.Purchase, pid)
            assert (p.reserved_quota_bytes, p.reserved_duration_days) == (0, 0)
            assert user_ops._maybe_activate_reserved_purchase_renewal(db, p)
            assert p.quota_bytes == 0 and p.expire_at is None
            db.commit()

        # A raw adjustment with a package id is not a complete package:
        # omitted quota must never accidentally grant unlimited access.
        name, pid = customer(finite, quota=20 * GB)
        with env.Session() as db:
            p = db.get(models.Purchase, pid)
            user_ops.renew_purchase_core(db, p, add_days=30, package_id=finite)
            assert p.quota_bytes == 20 * GB and p.expire_at > dt.datetime.utcnow()
            db.commit()

        # A poller's usage update is allowed, but changing the source package is refused.
        name, pid = customer(finite, quota=20 * GB, usable=True)
        item, values = register(name, pid, unlimited)
        with env.Session() as db:
            db.get(models.Purchase, pid).used_bytes = GB
            db.commit()
        assert not isinstance(execute(name, pid, unlimited, item, values), str)
        name, pid = customer()
        item, values = register(name, pid, finite)
        with env.Session() as db:
            db.get(models.Purchase, pid).package_id = unlimited
            db.commit()
        assert execute(name, pid, finite, item, values) == "target_purchase_changed"
        assert env.approval(item["approval_uuid"])["state"] == "cancelled"
        with env.Session() as db:
            assert db.query(mp.ProvisioningOperation).filter_by(approval_uuid=item["approval_uuid"]).count() == 0
            assert db.query(models.LedgerEntry).filter_by(approval_uuid=item["approval_uuid"]).count() == 0
            assert db.execute(select(rv.receipt_approval_effects.c.id).where(
                rv.receipt_approval_effects.c.approval_uuid == item["approval_uuid"])).first() is None

        for change in ("delete", "move"):
            name, pid = customer()
            item, values = register(name, pid, finite)
            with env.Session() as db:
                p = db.get(models.Purchase, pid)
                if change == "delete":
                    db.delete(p)
                else:
                    p.user_id = env.user_id("victim")
                db.commit()
            assert isinstance(execute(name, pid, finite, item, values), str)
            with env.Session() as db:
                assert db.query(mp.ProvisioningOperation).filter_by(approval_uuid=item["approval_uuid"]).count() == 0
                assert db.query(models.LedgerEntry).filter_by(approval_uuid=item["approval_uuid"]).count() == 0

        # Existing approvals keep the old precondition; no source state is guessed.
        for source, succeeds in ((finite, True), (original, False)):
            name, pid = customer(source)
            item, values = register(name, pid, finite)
            with env.Session() as db:
                db.execute(rv.receipt_approvals.update().where(
                    rv.receipt_approvals.c.approval_uuid == item["approval_uuid"]).values(
                    target_purchase_id_snapshot=None, target_purchase_package_id_snapshot=None))
                db.commit()
            result = execute(name, pid, finite, item, values)
            assert (not isinstance(result, str)) == succeeds, result
            if not succeeds:
                assert result == "target_purchase_changed"

        # The production combination: shadow registration and a normal wallet.
        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="shadow", loyalty_timing_mode="legacy_activation"))
            db.execute(rv.wallet_runtime_state.update().values(phase="normal"))
            db.commit()
        name, pid = customer()
        complete(name, pid, finite)
        print("PASS", label, "package-changing renewals, replay, target fence, unlimited limits")


with tempfile.TemporaryDirectory(prefix="um-renew-package-") as directory:
    engine = fixture.sqlite_engine(os.path.join(directory, "t.db"))
    try:
        scenario("sqlite", engine)
    finally:
        engine.dispose()
url = os.environ.get("MARIADB_TEST_URL", "").strip()
if url:
    engine = scratch.claim(url)
    try:
        scenario("mariadb", engine)
    finally:
        scratch.release(engine)
elif os.environ.get("CI", "").lower() == "true":
    raise AssertionError("CI requires real MariaDB")
else:
    print("SKIP real MariaDB locally; mandatory in CI")
assert _no_network.attempts == []
