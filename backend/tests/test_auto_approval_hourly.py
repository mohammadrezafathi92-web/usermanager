"""P9b rolling window, durable refusal audit, replay and actual DB races.

No node or Telegram contact. SQLite and disposable real MariaDB in CI.
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
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-hourly-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
import _receipt_void_env as fixture
from sqlalchemy import func, select
from app import models, models_receipt_void as rv
from app.services import receipt_approval_auto as auto, receipt_approval_registration as reg
from app.services import receipt_approval_intent as ri, receipt_approval_runtime as runtime
from app.routers import receipt_approvals as router
from fastapi import HTTPException

checks = 0


def check(label, actual, expected=True):
    global checks
    assert actual == expected, f"{label}: {actual!r} != {expected!r}"
    checks += 1
    print("PASS", label)


def scenario(label, engine):
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        clock = [dt.datetime.utcnow().replace(microsecond=0)]
        stack.enter_context(patch.object(auto, "database_now", side_effect=lambda db: clock[0]))
        stack.enter_context(patch.object(auto, "local_hour", return_value=12))

        def mode(value):
            with env.Session() as db:
                result = runtime.set_requested_mode(db, auto_rate_limit_mode=value)
                db.commit()
                return result

        def intent(n, name="victim", **kw):
            return ri.ApprovalIntent(pending_source_instance_id="hourly-test", pending_local_id=n,
                                    kind="new", target_username=name, amount=kw.pop("amount", 1000),
                                    package_id=kw.pop("package_id", env.ids.shared), **kw)

        def begin(n, *, name="victim", manual=False, **kw):
            with env.Session() as db:
                try:
                    return reg.begin(db, env.internal, intent(n, name, **kw),
                                     approval_mode="manual" if manual else "auto",
                                     approved_by_telegram_id=1000 if manual else None)
                except reg.RegistrationRejected as exc:
                    return exc.status, exc.code, exc.retry_after_seconds

        def count(table):
            with env.Session() as db:
                return db.scalar(select(func.count()).select_from(table))

        check(label + " activation explicit", mode("enforced")["auto_rate_limit_mode"], "enforced")
        first = begin(1)
        check("first auto granted", first["created"])
        check("same pending replays", begin(1)["approval_uuid"], first["approval_uuid"])
        check("same pending one grant event", count(rv.auto_approval_rate_events), 1)
        before = count(rv.receipt_approvals)
        denied = begin(2)
        check("second auto 429", denied, (429, "approval_registration_refused:auto_approval_rate_limited", 3600))
        check("denial creates no approval", count(rv.receipt_approvals), before)
        with env.Session() as db:
            row = db.execute(select(rv.auto_approval_rate_events).order_by(
                rv.auto_approval_rate_events.c.id.desc())).mappings().first()
            check("429 audited without approval", (row["outcome"], row["approval_uuid"], row["retry_after_seconds"]),
                  ("rate_limited", None, 3600))
        check("manual is not limited", begin(2, manual=True)["created"])
        check("manual no extra rate event", count(rv.auto_approval_rate_events), 2)
        with env.Session() as db:
            db.add(models.User(username="same-subject", telegram_id=7001))
            db.query(models.User).filter_by(username="resc").one().telegram_id = 7001
            db.commit()
        check("another account same Telegram shares slot", begin(20, name="same-subject")[0], 429)
        check("same Telegram different tenant independent",
              begin(21, name="resc", package_id=env.ids.owned, amount=500)["created"])
        clock[0] += dt.timedelta(minutes=59, seconds=59, milliseconds=500)
        check("round up retry", begin(3)[2], 1)
        clock[0] += dt.timedelta(milliseconds=500)
        third = begin(3)
        check("exactly sixty minutes allowed", third["created"])
        check("a failed approval does not release grant", begin(4)[0], 429)
        with env.Session() as db:
            db.execute(rv.receipt_approvals.update().where(
                rv.receipt_approvals.c.approval_uuid == third["approval_uuid"]).values(state="failed"))
            db.commit()
        check("same failed pending retries without new slot", begin(3)["approval_uuid"], third["approval_uuid"])

        # Different subject, same tenant: not blocked by victim's window.
        check("another customer independent", begin(5, name="loyal")["created"])
        with env.Session() as db:
            bot = db.get(models.BotSettings, 1)
            bot.auto_approve_enabled = False
            db.commit()
        policy_count = count(rv.receipt_approvals)
        check("central policy denial 403", begin(6, name="renewer")[:2],
              (403, "approval_registration_refused:auto_approval_policy_denied"))
        check("policy denial no approval", count(rv.receipt_approvals), policy_count)
        with env.Session() as db:
            check("policy refusal audit", db.execute(select(rv.auto_approval_rate_events.c.outcome).order_by(
                rv.auto_approval_rate_events.c.id.desc())).scalar(), "policy_denied")
            db.get(models.BotSettings, 1).auto_approve_enabled = True
            db.commit()

        mode("off")
        latest = begin(7)
        check("off retains grant accounting", latest["created"])
        # Missing/stale subject must be seeded from immutable approvals.
        with env.Session() as db:
            db.execute(rv.auto_approval_subjects.delete())
            db.commit()
        mode("enforced")
        check("activation seeds recent grants", begin(8)[0], 429)
        mode("enforced")
        check("activation retry does not consume another slot", begin(7)["approval_uuid"], latest["approval_uuid"])
        mode("shadow")
        check("shadow logs without limiting", begin(8)["created"])
        with env.Session() as db:
            check("shadow event", db.execute(select(rv.auto_approval_rate_events.c.outcome).order_by(
                rv.auto_approval_rate_events.c.id.desc())).scalar(), "shadow_would_limit")
            db.get(models.PanelSettings, 1).ha_enabled = True
            db.commit()
        with env.Session() as db:
            try:
                runtime.set_requested_mode(db, auto_rate_limit_mode="enforced")
                raise AssertionError("HA activation should fail")
            except runtime.ModeChangeRejected as exc:
                check("HA guard", exc.code, "ha_requires_external_fencing")
                db.rollback()
        with env.Session() as db:
            db.get(models.PanelSettings, 1).ha_enabled = False
            db.commit()
        mode("enforced")
        # Endpoint errors preserve a safe structured detail and Retry-After.
        with env.Session() as db:
            payload = router.ApprovalIntentIn(**{
                "pending_source_instance_id": "hourly-test", "pending_local_id": 9,
                "kind": "new", "target_username": "victim", "amount": 1000, "package_id": env.ids.shared})
            try:
                router.begin_approval(db, env.internal, payload, "auto")
                raise AssertionError("should be limited")
            except HTTPException as exc:
                check("HTTP429 header", (exc.status_code, exc.headers), (429, {"Retry-After": "3600"}))
                check("no prior approval leaked", sorted(exc.detail), ["error", "retry_after_seconds"])
        with patch.object(ri, "build_manifest", side_effect=RuntimeError("unavailable")):
            check("required failure never falls back to legacy", begin(10, name="renewer")[0], 503)

        clock[0] += dt.timedelta(minutes=61)
        barrier = threading.Barrier(2)
        results, errors = [], []

        def worker(n):
            try:
                barrier.wait(timeout=15)
                results.append(begin(n, name="renewer"))
            except BaseException as exc:
                errors.append(type(exc).__name__)

        workers = [threading.Thread(target=worker, args=(n,)) for n in (101, 102)]
        for t in workers:
            t.start()
        for t in workers:
            t.join(45)
        check("race threads end", any(t.is_alive() for t in workers), False)
        check("race no unhandled exceptions", errors, [])
        check("two simultaneous requests one grant", sum(isinstance(r, dict) for r in results), 1)
        check("other request clean429", [r[0] for r in results if isinstance(r, tuple)], [429])


with tempfile.TemporaryDirectory(prefix="um-hourly-") as folder:
    engine = fixture.sqlite_engine(os.path.join(folder, "test.db"))
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
check("no network", _no_network.attempts, [])
print(f"all {checks} checks passed")
