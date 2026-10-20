"""Regressions of the PR #154 review (they fail on its first head 40f9a29).
SQLite and real MariaDB; fake runner. Only APIs both heads have.

  a) a queue consumption decides on the committed queue under the row lock,
     never on a stale ORM copy (another session changed 20/30 to 50/60);
  b) a new purchase given a deleted one's id in the same second inherits
     none of its cutover history; a new customer with a deleted one's id
     and username never matches a decision made for the old one;
  c) a retire decision's delivery claim binds the claimed service's expiry,
     status and cap (usage does not invalidate it).
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-p9a-review-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from app import models
from app.services import loyalty_activation, product_decisions, provisioning_readiness
from app.services import reservation_resolution as resolution, reservation_transition, user_ops, wallet_accounts
import _receipt_void_env as fixture

GB = 1024 ** 3
failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def code(call):
    try:
        call()
        return "ok"
    except HTTPException as exc:
        return exc.detail if isinstance(exc.detail, str) else exc.detail.get("error")


def scenario(label, engine):
    L = f"[{label}] "
    mariadb = engine.dialect.name != "sqlite"
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack, activate=False)
        Session, root = env.Session, env.root
        seq = iter(range(9600, 9700))

        def customer(*, reserved=None, user_reserved=None, created=None, username=None, ident=None):
            tg = next(seq)
            with Session() as db:
                values = {"id": ident} if ident is not None else {}
                user = wallet_accounts.create_user_with_wallet(db, username=username or f"rr{tg}", telegram_id=tg, **values)
                db.flush()
                purchase = models.Purchase(user_id=user.id, package_id=env.ids.shared, quota_bytes=10 * GB, used_bytes=GB,
                                           max_concurrent_sessions=1, status=models.UserStatus.active,
                                           expire_at=dt.datetime.utcnow() + dt.timedelta(days=5),
                                           created_at=created or dt.datetime.utcnow())
                for key, value in (reserved or {}).items():
                    setattr(purchase, key, value)
                db.add(purchase)
                for key, value in (user_reserved or {}).items():
                    setattr(user, key, value)
                db.commit()
                return user.id, user.username, purchase.id

        # a) the committed queue decides
        _, _, pid = customer(reserved=dict(reserved_quota_bytes=20 * GB, reserved_duration_days=30))
        with Session() as first:
            stale = first.get(models.Purchase, pid)
            _ = (stale.reserved_quota_bytes, stale.reserved_duration_days, stale.reservation_activation_seq)
            first.expire_on_commit = False
            with Session() as second:
                fresh = second.get(models.Purchase, pid)
                fresh.reserved_quota_bytes, fresh.reserved_duration_days = 50 * GB, 60
                second.commit()
            stale.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
            try:
                user_ops._maybe_activate_reserved_purchase_renewal(first, stale)
                first.commit()
            except OperationalError:
                first.rollback()                       # MariaDB: restart, then consume afresh
                with Session() as again:
                    p = again.get(models.Purchase, pid)
                    p.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
                    user_ops._maybe_activate_reserved_purchase_renewal(again, p)
                    again.commit()
        with Session() as db:
            p = db.get(models.Purchase, pid)
            check(L + "a) the committed 50 GB / 60 days queue applied, not the stale 20 / 30",
                  (p.quota_bytes, p.reserved_quota_bytes, p.expire_at > dt.datetime.utcnow() + dt.timedelta(days=59)),
                  (50 * GB, None, True))

        # a consumption's node reconciliation never holds SQLite's single writer
        if not mariadb:
            import sqlite3
            _, _, pw = customer(reserved=dict(reserved_quota_bytes=20 * GB, reserved_duration_days=30))
            probes = []

            def probe(_db, _purchase):
                other = sqlite3.connect(engine.url.database, timeout=0)
                try:
                    other.execute("BEGIN IMMEDIATE")
                    other.rollback()
                    probes.append("free")
                except sqlite3.OperationalError:
                    probes.append("locked")
                finally:
                    other.close()
            with Session() as db, patch.object(user_ops, "reconcile_purchase_connections", side_effect=probe):
                p = db.get(models.Purchase, pw)
                p.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
                p.status = models.UserStatus.expired
                check(L + "a queue consumed by the poller", user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller"),
                      True)
                db.commit()
            check(L + "...its node reconciliation ran with the writer free", probes, ["free"])

        # c) a delivery claim binds expiry, status and cap
        u_c, n_c, p_c = customer(user_reserved={"reserved_quota_bytes": 5 * GB, "reserved_duration_days": 30})
        with Session() as db:
            decision = resolution.decide(db, root, user_id=u_c, action="retire_residual",
                                         reason="owner decision: delivered as that service",
                                         delivered_by_purchase_ids=[p_c])
        for field, value in (("max_concurrent_sessions", 4), ("status", models.UserStatus.disabled),
                             ("expire_at", dt.datetime(2030, 1, 1))):
            with Session() as db:
                target = db.get(models.Purchase, p_c)
                old = getattr(target, field)
                setattr(target, field, value)
                db.commit()
            with Session() as db:
                check(L + f"c) the claimed service's {field} is bound",
                      code(lambda: resolution.preview(db, decision["decision_id"])), "state_changed")
                setattr(db.get(models.Purchase, p_c), field, old)
                db.commit()
        with Session() as db:
            db.get(models.Purchase, p_c).used_bytes = 7 * GB
            db.commit()
        with Session() as db:
            check(L + "c) usage alone does not invalidate the claim",
                  code(lambda: resolution.preview(db, decision["decision_id"])), "ok")

        # b) a reused customer id + username (same second) never matches the old decision
        u_b, n_b, p_b = customer(user_reserved={"reserved_package_id": env.ids.shared})
        with Session() as db:
            decision_b = resolution.decide(db, root, user_id=u_b, action="retire_residual",
                                           reason="owner decision for the customer that will be deleted")
            user = db.get(models.User, u_b)
            tg = user.telegram_id
            for conn in db.query(models.Connection).filter_by(user_id=u_b).all():
                db.delete(conn)
            db.delete(db.get(models.Purchase, p_b))
            db.commit()
        with Session() as db:
            user = db.get(models.User, u_b)
            account = wallet_accounts.prepare_user_deletion(db, user)
            wallet_accounts.tombstone_user_account(db, user, account_id=account)
            db.execute(models.User.__table__.delete().where(models.User.id == u_b))
            db.commit()
        with Session() as db:
            again = wallet_accounts.create_user_with_wallet(db, id=u_b, username=n_b, telegram_id=tg,
                                                            reserved_package_id=env.ids.shared)
            db.flush()
            db.add(models.Purchase(id=p_b, user_id=again.id, package_id=env.ids.shared, quota_bytes=10 * GB,
                                   used_bytes=GB, max_concurrent_sessions=1, status=models.UserStatus.active,
                                   expire_at=dt.datetime.utcnow() + dt.timedelta(days=5)))
            db.commit()
        with Session() as db:
            check(L + "b) a new customer with the old id and username: the old decision does not match",
                  code(lambda: resolution.preview(db, decision_b["decision_id"])), "state_changed")
            db.query(models.User).filter(models.User.id == u_b).update({"reserved_package_id": None})
            db.query(models.User).filter(models.User.reserved_quota_bytes.isnot(None)).update(
                {"reserved_quota_bytes": None, "reserved_duration_days": None}, synchronize_session=False)
            db.commit()

        # b) a reused purchase id in the same second inherits no cutover history
        created = dt.datetime.utcnow().replace(microsecond=0)
        u_s, _, p_s = customer(reserved=dict(reserved_quota_bytes=20 * GB, reserved_duration_days=30), created=created)
        with Session() as db:
            product_decisions.confirm(db, root.id, version=product_decisions.VERSION,
                                      content_hash=product_decisions.CONTENT_HASH)
        with Session() as db, patch.object(provisioning_readiness, "p6_ready", return_value=(True, [])):
            check(L + "activation", bool(loyalty_activation.activate(db, root.id)["activated"]), True)
        with Session() as db:
            db.delete(db.get(models.Purchase, p_s))
            db.commit()
        with Session() as db:
            db.add(models.Purchase(id=p_s, user_id=u_s, package_id=env.ids.shared, quota_bytes=GB, used_bytes=0,
                                   status=models.UserStatus.active, created_at=created))
            db.commit()
        with Session() as db:
            check(L + "b) a new purchase with the old id in the same second inherits no snapshot",
                  reservation_transition.snapshots_of(db, [db.get(models.Purchase, p_s)]), [])
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-p9a-review-") as directory:
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
check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
