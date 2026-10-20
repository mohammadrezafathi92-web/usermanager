"""Renewal voids across sequences and between the void's own steps (review
of PR #154 head 411adaa; these fail there). SQLite and real MariaDB; fake
runner, no network.

  1. a second void counts the first one: two renewals in one queue voided in
     both orders; void -> the queue starts -> void the other (its term is not
     taken as shared with the voided one);
  2. a queue consumption between the void's steps: after the block step the
     poller and RADIUS do not start the queue (the void holds it) and a
     renewal of that service is refused until the renewal step committed;
     the void then completes; after it, the queue starts normally; a stop
     after any step resumes without a second effect;
  3. a loyalty quota reward of the same receipt, partly used: the loyalty step
     takes back only its unused part (the rest is its own shortfall) and the
     renewal step recognises that, restores the previous term, and records
     the voided term's usage apart - no debt, no refund, no second effect.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-void-seq-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import func, select

from app import models, models_receipt_void as rv, models_reservation_transition as rt, schemas
from app.routers import bot as bot_router
from app.services import loyalty_service, receipt_void_engine as void_engine, receipt_void_runner as runner
from app.services import user_ops
import _receipt_void_env as fixture

GB = 1024 ** 3
failures = []


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
        Session = env.Session
        tgs = iter(range(8500, 8999))
        SHARED = env.ids.shared

        def customer():
            tg = next(tgs)
            name = f"vs{tg}"
            env.new_user(name, tg)
            with Session() as db:
                user = db.query(models.User).filter_by(username=name).one()
                return name, db.query(models.Purchase).filter_by(user_id=user.id).one().id

        def renew(name, pid):
            item = env.begin("renew", name, 1000, package_id=SHARED, renew_purchase_id=pid)
            result = env.run(lambda db: bot_router.renew_service(name, pid, schemas.BotRenewRequest(
                add_gb=10, add_days=30, package_id=SHARED, paid_amount=1000, payment_method="card",
                approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=1000),
                db=db, owner_admin_id=None, principal=env.internal))
            return item["approval_uuid"], result

        def update(pid, **values):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                for key, value in values.items():
                    setattr(p, key, value)
                db.commit()

        def consume(pid, trigger="poller"):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                p.used_bytes = max(int(p.used_bytes or 0), int(p.quota_bytes or 0))
                applied = user_ops._maybe_activate_reserved_purchase_renewal(db, p, trigger)
                db.commit()
                return applied

        def row(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return {"q": int(p.quota_bytes or 0), "u": int(p.used_bytes or 0), "rq": p.reserved_quota_bytes,
                        "rd": p.reserved_duration_days, "seq": int(p.reservation_activation_seq or 0),
                        "st": getattr(p.status, "value", p.status), "e": p.expire_at}

        def core_renewal(pid):
            """Any other renewal path (panel, bulk): the core itself refuses."""
            from fastapi import HTTPException
            with Session() as db:
                try:
                    user_ops.renew_purchase_core(db, db.get(models.Purchase, pid), add_gb=1, add_days=1)
                    db.rollback()
                    return "renewed"
                except HTTPException as exc:
                    db.rollback()
                    return exc.detail

        def full_void(approval_uuid, name):
            shown = env.preview(approval_uuid)
            if isinstance(shown, tuple):
                return shown
            return env.void(shown["preview_token"], name)

        def reversal(approval_uuid):
            with Session() as db:
                r = db.execute(select(rt.receipt_void_renewal_reversals).where(
                    rt.receipt_void_renewal_reversals.c.approval_uuid == approval_uuid)).mappings().first()
                return None if r is None else (r["reversal_case"], r["consumed_bytes"], r["set_aside_bytes"], r["unknown_bytes"])

        # ---------------------------------------------------------------- 1. a second void counts the first
        for first_is_a in (True, False):
            n, p = customer()
            (a, _), (b, _) = renew(n, p), renew(n, p)
            first, second = (a, b) if first_is_a else (b, a)
            done1 = full_void(first, n)
            done2 = full_void(second, n)
            check(L + f"1. two queued renewals voided ({'A then B' if first_is_a else 'B then A'}): both complete, "
                  "the queue empty", (done1.get("state"), done2.get("state") if isinstance(done2, dict) else done2,
                                      row(p)["rq"], row(p)["rd"]), ("completed", "completed", None, None))
        n, p = customer()
        (a, _), (b, _) = renew(n, p), renew(n, p)
        full_void(a, n)
        consume(p)                                                      # B alone starts
        update(p, used_bytes=4 * GB)
        done = full_void(b, n)
        check(L + "1. void A, the queue starts, void B: B's term is B's alone (not shared with the voided A)",
              (done.get("state") if isinstance(done, dict) else done, reversal(b)),
              ("completed", ("current_term", 4 * GB, 6 * GB, None)))

        # ---------------------------------------------------------------- 2. consumption between the void's steps
        n, p = customer()
        (a, _), (b, _) = renew(n, p), renew(n, p)
        shown = env.preview(a)
        with Session() as db:
            sale_a = db.query(models.LedgerEntry).filter_by(approval_uuid=a, kind="sale_renew").one().id
        started = void_engine.execute(Session, env.root, preview_token=shown["preview_token"], typed_username=n,
                                      reason="fake receipt", danger_ack=True, ip="203.0.113.9", budget=0.0)
        op = started["operation_id"]
        seen = []
        for _ in range(12):                                            # one step at a time, a "crash" in between
            steps = env.steps_of(op)
            if all(state == "done" for state, _ in steps.values()):
                break
            if steps.get("block", ("",))[0] == "done" and steps.get("renewal_reversal", ("",))[0] != "done":
                seen.append((consume(p, "poller"), consume(p, "radius"), renew(n, p)[1], core_renewal(p)))
            runner.advance(Session, op, budget=0.0)
        with Session() as db:
            state = db.execute(select(rv.wallet_operations.c.state).where(rv.wallet_operations.c.id == op)).scalar()
            voided = db.get(models.LedgerEntry, sale_a).voided_at is not None
        check(L + "2. between the void's steps the queue does not start (poller, RADIUS); the bot is refused by the "
              "fraud hold, and any other renewal of the service by the void's hold",
              bool(seen) and all(s[:2] == (False, False) and str(s[2]).startswith("receipt_void:")
                                 and s[3] == "purchase_void_in_progress" for s in seen), True)
        check(L + "2. ...the void completed after stops between its steps; the sale voided once; only A left the queue",
              (state, voided, row(p)["rq"], (reversal(a) or (None,))[0]), ("completed", True, 10 * GB, "queued"))
        check(L + "2. ...after it the queue starts normally", consume(p), True)

        # ---------------------------------------------------------------- 3. a partly used quota reward of the receipt
        with Session() as db:
            loyalty_service.change_policy(db, env.ids.root, loyalty_purchase_threshold=2, loyalty_reward_credit=0,
                                          loyalty_reward_gb=10)
            db.commit()
        n, p = customer()                                               # its first counted purchase
        update(p, used_bytes=10 * GB)                                   # exhausted: the renewal applies at once
        r, _ = renew(n, p)
        after_renewal = row(p)
        check(L + "3. an immediate renewal of 10 GB with its own 10 GB loyalty reward: 20 GB",
              after_renewal["q"], 20 * GB)
        update(p, used_bytes=15 * GB)
        done = full_void(r, n)
        with Session() as db:
            reward = db.execute(select(rv.loyalty_reward_events).where(
                rv.loyalty_reward_events.c.causation_approval_uuid == r)).mappings().one()
            debts = db.execute(select(func.count()).select_from(rv.wallet_debts)).scalar()
            refunds = db.query(models.LedgerEntry).filter(models.LedgerEntry.approval_uuid == r,
                                                          models.LedgerEntry.kind.notin_(("sale_renew", "receipt_void"))).count()
        final = row(p)
        check(L + "3. the void completes: the loyalty step took back the unused 5 GB of the reward, the renewal step "
              "recognised it", (done.get("state") if isinstance(done, dict) else done, reward["quota_reversed_bytes"]),
              ("completed", 5 * GB))
        check(L + "3. ...the previous term (10 GB, its own 10 GB usage) comes back; still exhausted",
              (final["q"], final["u"], final["st"]), (10 * GB, 10 * GB, "quota_exceeded"))
        check(L + "3. ...the voided term's usage apart (10 GB of the renewal); no debt, no refund",
              (reversal(r), debts, refunds), (("current_term", 10 * GB, 0, None), 0, 0))
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-void-seq-") as directory:
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
