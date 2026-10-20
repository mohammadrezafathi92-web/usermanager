"""Void of a renewal receipt takes back only that payment's right on that
service (owner decision 2026-10-20). SQLite and real MariaDB; fake runner.

  queued      sole / aggregate with different packages / unlimited: the queue
              rebuilt from the other payments, their order and meaning kept;
  current     a consumed term: the previous limits and expiry (the real one),
              the previous term's own usage; the voided term's consumption
              recorded apart, never moved; a later queued renewal kept;
  immediate   with a package change, over an expired term: package, cap,
              quota, expiry and usage of before come back, still expired;
  superseded  a later legitimate term: the service untouched; the voided
              term's consumption recorded - or UNKNOWN when it was shared;
  fail closed a shared current term, a term changed outside the history, a
              renewal without history: refused at preview and at execute;
  and: nothing deleted or disabled, other services untouched, no refund or
  customer credit, the sale voided, one reversal row, a repeat replays.
"""
import contextlib
import datetime as dt
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-void-renewal-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from sqlalchemy import func, select

from app import models, models_receipt_void as rv, models_reservation_transition as rt, schemas
from app.routers import bot as bot_router
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
        tgs = iter(range(8100, 8999))
        with Session() as db:
            capped = models.Package(name="20 GB, three users", quota_gb=20, duration_days=30, price=2000,
                                    max_concurrent_sessions=3)
            unl = models.Package(name="Unlimited quota", quota_gb=0, duration_days=30, price=3000, max_concurrent_sessions=2)
            db.add_all([capped, unl])
            db.flush()
            for package in (capped, unl):
                db.add(models.PackageConnection(package_id=package.id, node_id=env.ids.node, protocol="wireguard"))
            db.get(models.Package, env.ids.shared).max_concurrent_sessions = 1
            db.commit()
            SHARED, CAPPED, UNL = env.ids.shared, capped.id, unl.id
        PKG = {SHARED: (10, 30, 1000), CAPPED: (20, 30, 2000), UNL: (0, 30, 3000)}

        def customer():
            tg = next(tgs)
            name = f"vr{tg}"
            env.new_user(name, tg)
            with Session() as db:
                user = db.query(models.User).filter_by(username=name).one()
                return name, db.query(models.Purchase).filter_by(user_id=user.id).one().id

        def renew(name, pid, package):
            gb, days, price = PKG[package]
            item = env.begin("renew", name, price, package_id=package, renew_purchase_id=pid)
            env.run(lambda db: bot_router.renew_service(name, pid, schemas.BotRenewRequest(
                add_gb=gb, add_days=days, package_id=package, paid_amount=price, payment_method="card",
                approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=price),
                db=db, owner_admin_id=None, principal=env.internal))
            check(L + f"renewal of P{pid} completed", env.approval(item["approval_uuid"])["state"], "completed")
            return item["approval_uuid"]

        def update(pid, **values):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                for key, value in values.items():
                    setattr(p, key, value)
                db.commit()

        def consume(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                p.used_bytes = max(int(p.used_bytes or 0), int(p.quota_bytes or 0))     # the term ran out
                applied = user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller")
                db.commit()
                return applied

        def row(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return {"q": int(p.quota_bytes or 0), "u": int(p.used_bytes or 0), "e": p.expire_at, "pk": p.package_id,
                        "cap": p.max_concurrent_sessions, "st": getattr(p.status, "value", p.status),
                        "rq": p.reserved_quota_bytes, "rd": p.reserved_duration_days, "rp": p.reserved_package_id,
                        "rcap": p.reserved_max_concurrent_sessions, "rrl": p.reserved_replace_package_limits}

        def void(approval_uuid, name):
            shown = env.preview(approval_uuid)
            if isinstance(shown, tuple):
                return shown, None
            return env.void(shown["preview_token"], name), shown

        def reversal(approval_uuid):
            with Session() as db:
                r = db.execute(select(rt.receipt_void_renewal_reversals).where(
                    rt.receipt_void_renewal_reversals.c.approval_uuid == approval_uuid)).mappings().first()
                return None if r is None else (r["reversal_case"], r["consumed_bytes"], r["set_aside_bytes"], r["unknown_bytes"])

        # ---------------------------------------------------------------- queued, sole; another service kept
        n1, p1 = customer()
        env.purchase(n1, None, amount=1000)
        with Session() as db:
            other = [p.id for p in db.query(models.Purchase).filter(models.Purchase.user_id == db.query(
                models.User).filter_by(username=n1).one().id, models.Purchase.id != p1)][0]
        before_other, before_p1 = row(other), row(p1)
        r1 = renew(n1, p1, CAPPED)
        done, shown = void(r1, n1)
        after_p1 = row(p1)
        check(L + "queued, sole: the queue removed, the service's limits and usage unchanged",
              (done["state"], after_p1["rq"], after_p1["rp"], after_p1["rcap"],
               (after_p1["q"], after_p1["u"], after_p1["e"], after_p1["pk"], after_p1["cap"]) ==
               (before_p1["q"], before_p1["u"], before_p1["e"], before_p1["pk"], before_p1["cap"])),
              ("completed", None, None, None, True))
        with Session() as db:
            user = db.query(models.User).filter_by(username=n1).one()
            enabled = db.query(models.Connection).filter_by(user_id=user.id, enabled=True).count()
            total = db.query(models.Connection).filter_by(user_id=user.id).count()
            sale = db.query(models.LedgerEntry).filter_by(approval_uuid=r1, kind="sale_renew").one()
            refunds = db.query(models.LedgerEntry).filter(models.LedgerEntry.approval_uuid == r1,
                                                          models.LedgerEntry.kind.notin_(("sale_renew", "receipt_void"))).count()
            sources = db.execute(select(func.count()).select_from(rv.wallet_credit_sources).where(
                rv.wallet_credit_sources.c.causation_approval_uuid == r1,
                rv.wallet_credit_sources.c.origin_kind != "loyalty_reward")).scalar()
        check(L + "...nothing deleted or disabled; the other service untouched",
              (user.status.value, enabled == total, row(other) == before_other), ("active", True, True))
        check(L + "...the sale voided; no refund entry and no credit to the customer",
              (sale.voided_at is not None, refunds, sources), (True, 0, 0))
        check(L + "...one reversal row", reversal(r1), ("queued", None, None, None))
        again = env.void(shown["preview_token"], n1)
        check(L + "...a repeat returns the same operation, nothing twice",
              (again.get("replayed"), again["operation_id"] == done["operation_id"], reversal(r1)[0]), (True, True, "queued"))

        # ---------------------------------------------------------------- queued, aggregate with different packages
        for void_first in (True, False):
            n, p = customer()
            ra, rb = renew(n, p, CAPPED), renew(n, p, SHARED)
            target, keep = (ra, SHARED) if void_first else (rb, CAPPED)
            void(target, n)
            gb, days, _ = PKG[keep]
            r = row(p)
            check(L + f"queued, aggregate: void the {'first' if void_first else 'second'} renewal -> the other's queue",
                  (r["rq"], r["rd"], r["rp"], r["rcap"], bool(r["rrl"])),
                  (gb * GB, days, keep, 3 if keep == CAPPED else 1, True))

        # ---------------------------------------------------------------- queued, unlimited in the queue
        for void_first in (True, False):
            n, p = customer()
            ra, rb = renew(n, p, UNL), renew(n, p, SHARED)
            check(L + "the unlimited queue stays unlimited with a finite package added",
                  (row(p)["rq"], row(p)["rd"]), (0, 60))
            void(ra if void_first else rb, n)
            r = row(p)
            expected = (10 * GB, 30, SHARED, 1, True) if void_first else (0, 30, UNL, 2, True)
            check(L + f"queued, unlimited: void the {'unlimited' if void_first else 'finite'} renewal -> rebuilt, not subtracted",
                  (r["rq"], r["rd"], r["rp"], r["rcap"], bool(r["rrl"])), expected)

        # ---------------------------------------------------------------- current term after the queue started
        n6, p6 = customer()
        prev = row(p6)
        r6 = renew(n6, p6, CAPPED)
        check(L + "the queue starts when the term runs out", consume(p6), True)
        update(p6, used_bytes=5 * GB)                                       # consumed from the voided term
        r6b = renew(n6, p6, SHARED)                                        # a later, legitimate queued renewal
        done, shown = void(r6, n6)
        r = row(p6)
        check(L + "current term: the previous quota, the REAL previous expiry, package and cap come back",
              (done["state"], r["q"], r["e"] == prev["e"], r["pk"], r["cap"]),
              ("completed", 10 * GB, True, SHARED, prev["cap"]))
        check(L + "...the previous term's own usage; the voided term's 5 GB recorded apart (15 GB set aside)",
              (r["u"], reversal(r6)), (10 * GB, ("current_term", 5 * GB, 15 * GB, None)))
        check(L + "...the previous term had ended: it is not revived (quota_exceeded); the later queue is kept",
              (r["st"], r["rq"], r["rp"]), ("quota_exceeded", 10 * GB, SHARED))
        check(L + "...the later renewal is still completed", env.approval(r6b)["state"], "completed")

        # ---------------------------------------------------------------- immediate, package change, over an expired term
        n7, p7 = customer()
        past = dt.datetime.utcnow().replace(microsecond=0) - dt.timedelta(days=1)
        update(p7, used_bytes=6 * GB, expire_at=past, status=models.UserStatus.expired)
        r7 = renew(n7, p7, CAPPED)
        check(L + "an expired service renews at once with the new package", (row(p7)["q"], row(p7)["pk"], row(p7)["cap"]),
              (20 * GB, CAPPED, 3))
        update(p7, used_bytes=3 * GB)
        void(r7, n7)
        r = row(p7)
        check(L + "immediate: quota, expiry, package, cap and the 6 GB usage of before; still expired",
              (r["q"], r["e"] == past, r["pk"], r["cap"], r["u"], r["st"]),
              (10 * GB, True, SHARED, 1, 6 * GB, "expired"))
        check(L + "...the voided term's 3 GB recorded apart, not added to the 6 GB",
              reversal(r7), ("current_term", 3 * GB, 17 * GB, None))

        # ---------------------------------------------------------------- superseded: sole, then shared
        n8, p8 = customer()
        update(p8, used_bytes=10 * GB)
        r8 = renew(n8, p8, SHARED)                                         # immediate: the term it made
        update(p8, used_bytes=10 * GB)
        r8b = renew(n8, p8, SHARED)                                        # a later legitimate term
        update(p8, used_bytes=2 * GB)
        before = row(p8)
        void(r8, n8)
        check(L + "superseded: the current (legitimate) term untouched; the voided term's consumption recorded",
              (row(p8) == before, reversal(r8), env.approval(r8b)["state"]),
              (True, ("superseded", 10 * GB, 0, None), "completed"))
        n9, p9 = customer()
        r9a, r9b = renew(n9, p9, SHARED), renew(n9, p9, SHARED)
        consume(p9)                                                        # one term from both payments
        update(p9, used_bytes=20 * GB)
        renew(n9, p9, SHARED)                                              # replaced by a later term
        void(r9a, n9)
        check(L + "superseded, shared term: its usage recorded as UNKNOWN, never as consumed",
              reversal(r9a), ("superseded", None, None, 20 * GB))

        # ---------------------------------------------------------------- fail closed
        n10, p10 = customer()
        r10a, r10b = renew(n10, p10, SHARED), renew(n10, p10, SHARED)
        consume(p10)
        before = row(p10)
        shown = env.preview(r10a)
        check(L + "a current term shared with another payment: refused at preview, with the reason",
              (shown["resources"]["renewal"].get("refused"), "renewal_term_shared" in shown["blockers"]),
              ("renewal_term_shared", True))
        check(L + "...and at execute; nothing changed", (env.void(shown["preview_token"], n10), row(p10) == before),
              ((409, "renewal_term_shared"), True))
        n11, p11 = customer()
        r11 = renew(n11, p11, SHARED)
        consume(p11)
        update(p11, quota_bytes=11 * GB)                                    # changed outside the history
        shown = env.preview(r11)
        check(L + "a term changed outside the history: refused", shown["resources"]["renewal"].get("refused"),
              "renewal_term_changed")
        n12, p12 = customer()
        r12 = renew(n12, p12, SHARED)
        with Session() as db:
            db.execute(rt.purchase_renewal_contributions.delete().where(
                rt.purchase_renewal_contributions.c.approval_uuid == r12))
            db.commit()
        shown = env.preview(r12)
        check(L + "a renewal without its history: refused, never guessed",
              shown["resources"]["renewal"].get("refused"), "renewal_history_unavailable")
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-void-renewal-") as directory:
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
