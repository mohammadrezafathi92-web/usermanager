"""A queue consumption that loses its commit-time check (review of 5ca8965;
fails there). Two real Sessions, deterministic order; the two causes apart:

  sequence changed  both Sessions saw the old term exhausted and consumed the
                    queue; the first commits. The second must not write
                    anything of the ended term onto the winner's:
                      - with its own usage on the purchase (the review's
                        reproduction): its commit is refused
                        (ConsumptionConflict) and it rolls back - the winner's
                        term stays (usage 0), one consumption recorded; a retry
                        on the committed state consumes nothing again;
                      - without own changes to the purchase (a RADIUS login):
                        its consumption is dropped, its other changes commit,
                        the purchase reads the winner's term, one reconcile;
  hold              a renewal void's hold committed after the check: the same
                    term is still current, so the purchase is put back as the
                    transaction had it - its own usage kept, the queue kept.

SQLite: both consumers pass their checks before either commits. MariaDB: the
row lock makes the second wait for the first; it then sees the moved
sequence and consumes nothing (no stale write either). Fake runner, no network.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-consume-conflict-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import models, models_reservation_transition as rt, schemas
from app.routers import bot as bot_router
from app.services import reservation_transition, user_ops
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def scenario(label, engine):
    L = f"[{label}] "
    mariadb = engine.dialect.name != "sqlite"
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        Session = env.Session
        tgs = iter(range(9100, 9399))

        def customer_with_queue():
            tg = next(tgs)
            name = f"cc{tg}"
            env.new_user(name, tg)
            with Session() as db:
                user = db.query(models.User).filter_by(username=name).one()
                pid = db.query(models.Purchase).filter_by(user_id=user.id).one().id
            item = env.begin("renew", name, 1000, package_id=env.ids.shared, renew_purchase_id=pid)
            env.run(lambda db: bot_router.renew_service(name, pid, schemas.BotRenewRequest(
                add_gb=10, add_days=30, package_id=env.ids.shared, paid_amount=1000, payment_method="card",
                approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=1000),
                db=db, owner_admin_id=None, principal=env.internal))
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return name, pid, int(p.quota_bytes or 0), int(p.reserved_quota_bytes or 0)

        def row(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return (int(p.quota_bytes or 0), int(p.used_bytes or 0), p.reserved_quota_bytes,
                        int(p.reservation_activation_seq or 0))

        def logged(pid):
            with Session() as db:
                return [int(r.used_bytes_before) for r in db.execute(
                    select(rt.purchase_reservation_activations.c.used_bytes_before).where(
                        rt.purchase_reservation_activations.c.purchase_id == pid))]

        def exhaust(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                p.used_bytes, p.status = int(p.quota_bytes or 0), models.UserStatus.quota_exceeded
                db.commit()

        if mariadb:
            # the second consumer waits on the row lock, then sees the moved sequence
            name, pid, quota, reserved = customer_with_queue()
            first, second = Session(), Session()
            p1 = first.get(models.Purchase, pid)
            p2 = second.get(models.Purchase, pid)
            p1.used_bytes = p2.used_bytes = quota
            check(L + "first consumer consumes", user_ops._maybe_activate_reserved_purchase_renewal(first, p1, "poller"))
            first.flush()
            out = {}

            def run():
                try:
                    out["applied"] = user_ops._maybe_activate_reserved_purchase_renewal(second, p2, "poller")
                    second.commit()
                except OperationalError as exc:                  # snapshot isolation: restart
                    second.rollback()
                    out["applied"] = f"restart:{exc.orig.args[0]}"
            t = threading.Thread(target=run)
            t.start()
            threading.Event().wait(0.5)
            first.commit()
            t.join(60)
            first.close()
            second.close()
            check(L + "the second consumes nothing; the winner's term stays, one consumption",
                  (out.get("applied") in (False, "restart:1020"), row(pid), logged(pid)),
                  (True, (reserved, 0, None, 1), [quota]))
            print(f"DONE  {label}")
            return

        # ------------------------------------------------ sequence changed, own usage (the review's reproduction)
        name, pid, quota, reserved = customer_with_queue()
        first, second = Session(), Session()
        p1, p2 = first.get(models.Purchase, pid), second.get(models.Purchase, pid)
        p1.used_bytes = p2.used_bytes = quota                     # both see the old term run out
        applied = (user_ops._maybe_activate_reserved_purchase_renewal(first, p1, "poller"),
                   user_ops._maybe_activate_reserved_purchase_renewal(second, p2, "poller"))
        check(L + "seq changed + own usage: both consumers passed their checks", applied, (True, True))
        first.commit()
        after_first = row(pid)
        try:
            second.commit()
            outcome = "committed"
        except reservation_transition.ConsumptionConflict:
            second.rollback()
            outcome = "refused"
        first.close()
        second.close()
        check(L + "...the first commit: the new term", after_first, (reserved, 0, None, 1))
        check(L + "...the second commit is refused and rolled back", outcome, "refused")
        check(L + "...nothing of the ended term on the new one (usage 0); one consumption recorded",
              (row(pid), logged(pid)), ((reserved, 0, None, 1), [quota]))
        with Session() as db:                                     # the retry, on the committed state
            p = db.get(models.Purchase, pid)
            retried = user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller")
            db.commit()
        check(L + "...a retry consumes nothing again", (retried, row(pid), len(logged(pid))),
              (False, (reserved, 0, None, 1), 1))

        # ------------------------------------------------ sequence changed, no own change to the purchase
        name, pid, quota, reserved = customer_with_queue()
        exhaust(pid)
        calls = []
        with patch.object(user_ops, "reconcile_purchase_connections", side_effect=lambda _db, p: calls.append(p.id)):
            first, second = Session(), Session()
            p1, p2 = first.get(models.Purchase, pid), second.get(models.Purchase, pid)
            applied = (user_ops._maybe_activate_reserved_purchase_renewal(first, p1, "poller"),
                       user_ops._maybe_activate_reserved_purchase_renewal(second, p2, "radius"))
            second.get(models.User, p2.user_id).notes = "kept"    # another change of that transaction
            first.commit()
            second.flush()                                        # RADIUS: the guard decides here
            seen = (int(p2.quota_bytes or 0), int(p2.used_bytes or 0), p2.reserved_quota_bytes)
            second.commit()
            uid = p2.user_id
            first.close()
            second.close()
        with Session() as db:
            notes = db.get(models.User, uid).notes
        check(L + "seq changed, no own change: both passed their checks", applied, (True, True))
        check(L + "...the loser reads the winner's term after its flush (the login judged on it)",
              seen, (reserved, 0, None))
        check(L + "...its commit writes nothing stale; its other change kept; one consumption, one reconcile",
              (row(pid), logged(pid), notes, calls), ((reserved, 0, None, 1), [quota], "kept", [pid]))

        # ------------------------------------------------ hold committed after the check, own usage
        name, pid, quota, reserved = customer_with_queue()
        db = Session()
        p = db.get(models.Purchase, pid)
        p.used_bytes = quota
        check(L + "hold: the consumer passed its check", user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller"))
        with Session() as other:
            other.execute(rt.purchase_void_holds.insert().values(
                purchase_key=p.history_key, approval_uuid="00000000-0000-4000-8000-00000000c0c0", void_operation_id=1,
                state="active", created_at=dt.datetime.utcnow()))
            other.commit()
        db.commit()
        db.close()
        check(L + "...the same term stays: its own usage kept, the queue kept, nothing recorded",
              (row(pid), logged(pid)), ((quota, quota, reserved, 0), []))
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-consume-conflict-") as directory:
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
