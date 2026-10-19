"""The P9a reservation transition (addendum receipt-void-addendum-p9a-
reservations-2026-10-19.md). SQLite and real MariaDB; fake runner.

  * activation: refused while a User reservation exists, and before the
    Product decisions version is confirmed; a Purchase reservation does not
    block - it gets one immutable snapshot in the activation's transaction,
    and nothing about the reservation changes;
  * a transient MariaDB error (1213) rolls the whole activation back and runs
    it again from the start (no second epoch, no duplicate snapshot); on
    SQLite the error propagates and nothing is left;
  * race: a poller consumption that commits before the activation leaves no
    snapshot; one started while the activation holds its locks waits, then
    consumes, and its logged consumption is the snapshot's (by sequence);
  * consumption applies quota, days, package and cap exactly, counts nothing,
    and never applies the same reservation twice (stale second session);
  * history: a deleted purchase keeps its snapshot without an alert, a reused
    id never inherits it, a changed snapshot is reported;
  * Void (option A): the preview of a renewal void shows every purchase of the
    account with its queue and the cutover snapshots, bound to the token;
  * User reservation resolution: decision -> preview -> execute, the bound
    state, usage not bound, idempotency (replay after a later change, another
    request with the same key refused), one execution per decision under
    concurrency, refused after the activation.
"""
import contextlib
import datetime as dt
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
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-resv-transition-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app import models, models_receipt_void as rv, models_reservation_transition as rt, schemas
from app.routers import bot as bot_router
from app.services import loyalty_activation, loyalty_service, product_decisions, provisioning_readiness
from app.services import receipt_void_flags as flags, reservation_resolution as resolution
from app.services import reservation_transition, user_ops, wallet_accounts, wallet_reconcile
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


class Transient(Exception):
    """Stands in for pymysql's error: args[0] is the server's error code."""


def scenario(label, engine):
    L = f"[{label}] "
    mariadb = engine.dialect.name != "sqlite"
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack, activate=False)
        Session = env.Session
        ready = patch.object(provisioning_readiness, "p6_ready", return_value=(True, []))
        root = env.root
        seq = iter(range(9100, 9999))
        with Session() as db:
            capped = models.Package(name="Two users", quota_gb=20, duration_days=30, price=200, max_concurrent_sessions=3)
            db.add(capped)
            db.commit()
            CAPPED = capped.id

        def customer(*, reserved=None, user_reserved=None, usable=True, created=None):
            tg = next(seq)
            with Session() as db:
                user = wallet_accounts.create_user_with_wallet(db, username=f"rt{tg}", telegram_id=tg)
                db.flush()
                purchase = models.Purchase(user_id=user.id, package_id=env.ids.shared, quota_bytes=10 * GB,
                    used_bytes=GB, max_concurrent_sessions=1, status=models.UserStatus.active,
                    expire_at=dt.datetime.utcnow() + dt.timedelta(days=5 if usable else -1),
                    created_at=created or dt.datetime.utcnow())
                for key, value in (reserved or {}).items():
                    setattr(purchase, key, value)
                db.add(purchase)
                for key, value in (user_reserved or {}).items():
                    setattr(user, key, value)
                db.commit()
                return user.id, user.username, purchase.id

        def purchase_row(pid):
            with Session() as db:
                p = db.get(models.Purchase, pid)
                return None if p is None else reservation_transition.purchase_state(p) | {
                    "seq": int(p.reservation_activation_seq or 0), "used": int(p.used_bytes or 0)}

        def consume(pid, stale=None):
            with Session() as db:
                p = stale(db) if stale else db.get(models.Purchase, pid)
                p.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
                applied = user_ops._maybe_activate_reserved_purchase_renewal(db, p, "poller")
                db.commit()
                return applied

        QUEUE = dict(reserved_quota_bytes=20 * GB, reserved_duration_days=30, reserved_package_id=CAPPED,
                     reserved_max_concurrent_sessions=3, reserved_replace_package_limits=True,
                     reserved_created_at=dt.datetime(2026, 10, 10, 8, 0, 0))

        # ---------------------------------------------------------------- reservation resolution (before P9a)
        u_rp, n_rp, p_rp = customer(user_reserved={"reserved_package_id": env.ids.shared})
        u_tr, n_tr, p_tr = customer(usable=True, user_reserved={
            "reserved_quota_bytes": 20 * GB, "reserved_duration_days": 30, "reserved_package_id": CAPPED,
            "reserved_created_at": dt.datetime(2026, 8, 1, 5, 25, 49)})
        u_us, n_us, p_us = customer(user_reserved={"reserved_quota_bytes": 5 * GB, "reserved_duration_days": 30})
        with Session() as db:
            listed = {c["user_id"]: c["classification"] for c in resolution.candidates(db)}
        check(L + "candidates: every reserved customer, classification display only",
              (listed.get(u_rp), listed.get(u_tr), listed.get(u_us)), ("rp_only", "ambiguous", "ambiguous"))
        with Session() as db:
            check(L + "a transfer needs an explicit target and cap policy",
                  code(lambda: resolution.decide(db, root, user_id=u_tr, action="transfer_to_purchase",
                                                 reason="owner checked the bank record", target_purchase_id=p_tr)),
                  "transfer_requires_target_and_cap_policy")
            check(L + "no decision, no preview", code(lambda: resolution.preview(db, 999999)), "decision_not_found")
            transfer = resolution.decide(db, root, user_id=u_tr, action="transfer_to_purchase",
                                         reason="owner decision: paid renewal belongs to this service",
                                         target_purchase_id=p_tr, cap_policy="package_cap")
        with Session() as db:
            shown = resolution.preview(db, transfer["decision_id"])
        check(L + "preview: the exact after, the historical semantics and the cap shown",
              (shown["after"]["target"]["reserved_quota_bytes"], shown["after"]["target"]["reserved_max_concurrent_sessions"],
               shown["after"]["target"]["reserved_replace_package_limits"], any("historical" in w for w in shown["warnings"])),
              (20 * GB, 3, None, True))
        with Session() as db:
            check(L + "execute: the typed username must match",
                  code(lambda: resolution.execute(db, root, decision_id=transfer["decision_id"],
                                                  preview_token=shown["preview_token"], typed_username="nobody",
                                                  idempotency_key="k-transfer")), "typed_username_mismatch")
        # usage moves between preview and execute: not bound
        with Session() as db:
            db.get(models.Purchase, p_tr).used_bytes = 7 * GB
            db.commit()
        results = {}

        def run_execute(name, key, decision_id, token, username):
            with Session() as db:
                try:
                    results[name] = resolution.execute(db, root, decision_id=decision_id, preview_token=token,
                                                       typed_username=username, idempotency_key=key)
                except HTTPException as exc:
                    results[name] = exc.detail
        threads = [threading.Thread(target=run_execute, args=(n, k, transfer["decision_id"], shown["preview_token"], n_tr))
                   for n, k in (("a", "k-transfer"), ("b", "k-transfer-other"))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        outcomes = sorted("done" if isinstance(r, dict) and r.get("resolution") else r for r in results.values())
        check(L + "two executions of one decision at once: exactly one applies", outcomes,
              ["decision_already_executed", "done"])
        with Session() as db:
            user, target = db.get(models.User, u_tr), db.get(models.Purchase, p_tr)
            audits = db.execute(select(func.count()).select_from(rt.reservation_resolutions).where(
                rt.reservation_resolutions.c.user_id == u_tr)).scalar()
            moved = (user.reserved_quota_bytes, user.reserved_package_id, target.reserved_quota_bytes,
                     target.reserved_package_id, target.reserved_max_concurrent_sessions,
                     target.reserved_replace_package_limits, audits)
        check(L + "the transfer and its one audit row: the values verbatim, cap per policy, NULL replace flag",
              moved, (None, None, 20 * GB, CAPPED, 3, None, 1))
        winner = next(k for k, r in (("k-transfer", results["a"]), ("k-transfer-other", results["b"]))
                      if isinstance(r, dict) and r.get("resolution"))
        with Session() as db:
            db.get(models.Purchase, p_tr).comment = "changed later"
            db.commit()
        with Session() as db:
            again = resolution.execute(db, root, decision_id=transfer["decision_id"], preview_token="stale",
                                       typed_username=n_tr, idempotency_key=winner)
        with Session() as db:
            target = db.get(models.Purchase, p_tr)
            check(L + "the same request again, after a later change: the stored result, nothing re-applied",
                  (again.get("replayed"), again["resolution"], target.reserved_quota_bytes,
                   db.execute(select(func.count()).select_from(rt.reservation_resolutions)).scalar()),
                  (True, results["a"]["resolution"] if winner == "k-transfer" else results["b"]["resolution"], 20 * GB, 1))
            retire = resolution.decide(db, root, user_id=u_rp, action="retire_residual",
                                       reason="owner decision: package id left by the old conversion")
        with Session() as db:
            check(L + "the same key with another request: refused",
                  code(lambda: resolution.execute(db, root, decision_id=retire["decision_id"], preview_token="x",
                                                  typed_username=n_rp, idempotency_key=winner)), "idempotency_key_reused")
        with Session() as db:
            db.get(models.User, u_rp).reserved_package_id = env.ids.owned       # a decision-relevant change
            db.commit()
        with Session() as db:
            check(L + "a bound value changed since the decision: preview refused",
                  code(lambda: resolution.preview(db, retire["decision_id"])), "state_changed")
            db.get(models.User, u_rp).reserved_package_id = env.ids.shared
            db.commit()
        with Session() as db:
            shown = resolution.preview(db, retire["decision_id"])
            done = resolution.execute(db, root, decision_id=retire["decision_id"], preview_token=shown["preview_token"],
                                      typed_username=n_rp, idempotency_key="k-retire")
        with Session() as db:
            check(L + "retire_residual: the fields cleared, the services untouched",
                  (done["action"], db.get(models.User, u_rp).reserved_package_id,
                   db.get(models.Purchase, p_rp).quota_bytes), ("retire_residual", None, 10 * GB))
            last = resolution.decide(db, root, user_id=u_us, action="retire_residual",
                                     reason="owner decision recorded for the test customer")
        with Session() as db:
            shown = resolution.preview(db, last["decision_id"])
            resolution.execute(db, root, decision_id=last["decision_id"], preview_token=shown["preview_token"],
                               typed_username=n_us, idempotency_key="k-last")

        # ---------------------------------------------------------------- activation preconditions
        _, _, p_block = customer(user_reserved={"reserved_duration_days": 30})
        with Session() as db, ready:
            blocked = code(lambda: loyalty_activation.activate(db, root.id))
        check(L + "a User reservation still blocks the activation", blocked, "loyalty_cutover_blocked")
        with Session() as db:
            db.query(models.User).filter(models.User.reserved_duration_days.isnot(None)).update(
                {"reserved_duration_days": None}, synchronize_session=False)
            db.commit()
        with Session() as db, ready:
            check(L + "unconfirmed Product decisions: refused, nothing confirmed by default",
                  (code(lambda: loyalty_activation.activate(db, root.id)), product_decisions.confirmation(db)),
                  ("product_decisions_unconfirmed", None))

        u_a, n_a, p_a = customer(reserved=QUEUE)                         # consumed during the activation
        u_b, _, p_b = customer(reserved=QUEUE)                           # consumed before it
        u_c, _, p_c = customer(reserved=QUEUE, created=dt.datetime(2026, 10, 1, 9, 0, 0))   # deleted later
        u_d, _, p_d = customer(reserved=QUEUE)                           # two consumers
        u_v, n_v, p_v = customer(reserved=QUEUE)                          # its account's void preview
        with Session() as db:
            loyal = models.Purchase(user_id=u_v, package_id=env.ids.shared, quota_bytes=10 * GB, used_bytes=0,
                                    max_concurrent_sessions=1, status=models.UserStatus.active,
                                    expire_at=dt.datetime.utcnow() + dt.timedelta(days=20))
            db.add(loyal)
            db.commit()
            p_v2 = loyal.id
        check(L + "a consumption before the activation", consume(p_b), True)
        before_a = purchase_row(p_a)
        with Session() as db:
            product_decisions.confirm(db, root.id, version=product_decisions.VERSION,
                                      content_hash=product_decisions.CONTENT_HASH)
            counts_before = {u: db.get(models.User, u).purchase_count for u in (u_a, u_b, u_c, u_d)}

        calls = {"n": 0}
        paused, resume = threading.Event(), threading.Event()
        original = loyalty_service.capture_baselines

        def flaky_then_paused(db, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OperationalError("capture_baselines", {}, Transient(1213, "Deadlock found"))
            paused.set()
            resume.wait(30)
            return original(db, *args, **kwargs)

        activated, consumer = {}, {}

        def run_activation():
            with Session() as db:
                try:
                    activated["result"] = loyalty_activation.activate(db, root.id)
                except (HTTPException, OperationalError) as exc:
                    activated["result"] = exc

        with patch.object(loyalty_service, "capture_baselines", side_effect=flaky_then_paused), ready:
            if not mariadb:
                run_activation()
                with Session() as db:
                    leftovers = (db.execute(select(func.count()).select_from(rv.loyalty_policy_epochs)).scalar(),
                                 db.execute(select(func.count()).select_from(
                                     rt.loyalty_cutover_purchase_reservations)).scalar(),
                                 loyalty_service.runtime_row(db)["registration_mode"])
                check(L + "SQLite: a failed activation propagates and leaves nothing (no epoch, no snapshot, shadow)",
                      (isinstance(activated["result"], OperationalError), leftovers), (True, (0, 0, "shadow")))
            act = threading.Thread(target=run_activation)
            act.start()
            check(L + "the activation reached its locked section", paused.wait(30), True)
            cons = threading.Thread(target=lambda: consumer.update(result=consume(p_a)))
            cons.start()
            time.sleep(1.5)
            waited = cons.is_alive()
            resume.set()
            act.join(60)
            cons.join(60)
        result = activated["result"]
        check(L + "a consumption started during the activation waited for its commit, then applied",
              (waited, consumer.get("result")), (True, True))
        check(L + ("MariaDB: the 1213 rolled everything back and the activation ran again from the start"
                   if mariadb else "SQLite: the second activation committed"),
              (isinstance(result, dict) and result.get("activated"), calls["n"]), (True, 2))
        with Session() as db:
            snaps = {r["purchase_id"]: dict(r) for r in db.execute(select(rt.loyalty_cutover_purchase_reservations)).mappings()}
            epochs = db.execute(select(func.count()).select_from(rv.loyalty_policy_epochs)).scalar()
        # p_tr holds the reservation transferred to it above
        check(L + "one epoch; one snapshot per reservation that existed at the commit (not the one consumed before)",
              (epochs, sorted(snaps) == sorted([p_tr, p_a, p_c, p_d, p_v]), result["purchase_reservation_snapshots"]),
              (1, True, 5))
        check(L + "the snapshot is the reservation exactly, at its sequence",
              (snaps[p_a]["reserved_quota_bytes"], snaps[p_a]["reserved_package_id"],
               snaps[p_a]["reserved_max_concurrent_sessions"], snaps[p_a]["reservation_activation_seq"]),
              (20 * GB, CAPPED, 3, 0))
        after_a = purchase_row(p_a)
        check(L + "the consumption applied quota, days, package and cap exactly",
              (after_a["quota_bytes"], after_a["package_id"], after_a["max_concurrent_sessions"], after_a["seq"],
               after_a["reserved_package_id"], before_a["reserved_quota_bytes"]), (20 * GB, CAPPED, 3, 1, None, 20 * GB))
        with Session() as db:
            history = reservation_transition.snapshots_of(db, [db.get(models.Purchase, p_a)])
            events = db.execute(select(func.count()).select_from(rv.loyalty_purchase_events)).scalar()
            counts_after = {u: db.get(models.User, u).purchase_count for u in (u_a, u_b, u_c, u_d)}
        check(L + "the logged consumption is the snapshot's (by sequence, not time)",
              (len(history), history[0]["consumed"], history[0]["consumed_by_activation_id"] is not None), (1, True, True))
        check(L + "no loyalty event and no count from any consumption", (events, counts_after), (0, counts_before))

        # two consumers of one reservation: the stale second applies nothing
        with Session() as stale_db:
            stale = stale_db.get(models.Purchase, p_d)
            stale_db.expire_on_commit = False
            _ = stale.reservation_activation_seq
            first = consume(p_d)
            stale.expire_at = dt.datetime.utcnow() - dt.timedelta(seconds=1)
            try:
                second = user_ops._maybe_activate_reserved_purchase_renewal(stale_db, stale, "radius")
                stale_db.commit()
            except OperationalError as exc:
                # MariaDB (innodb_snapshot_isolation): the row changed since this
                # transaction read it -> 1020, the transaction must start over
                second = f"restart:{exc.orig.args[0]}"
                stale_db.rollback()
        with Session() as db:
            logged = db.execute(select(func.count()).select_from(rt.purchase_reservation_activations).where(
                rt.purchase_reservation_activations.c.purchase_id == p_d)).scalar()
            seq_d = db.get(models.Purchase, p_d).reservation_activation_seq
        check(L + "a reservation consumed by another session is not applied twice", (first, second, logged, seq_d),
              (True, "restart:1020" if mariadb else False, 1, 1))

        # history: deletion, id reuse, tampering
        with Session() as db:
            check(L + "reconcile: clean", wallet_reconcile.check(db)["problems"], [])
        with Session() as db:
            for conn in db.query(models.Connection).filter_by(purchase_id=p_c).all():
                db.delete(conn)
            db.delete(db.get(models.Purchase, p_c))
            db.commit()
        with Session() as db:
            reused = models.Purchase(id=p_c, user_id=u_c, package_id=env.ids.shared, quota_bytes=GB, used_bytes=0,
                                     status=models.UserStatus.active, created_at=dt.datetime(2026, 11, 1, 9, 0, 0),
                                     reserved_duration_days=30, reservation_activation_seq=3)
            db.add(reused)
            db.commit()
            kept = db.execute(select(func.count()).select_from(rt.loyalty_cutover_purchase_reservations).where(
                rt.loyalty_cutover_purchase_reservations.c.purchase_id == p_c)).scalar()
            inherited = reservation_transition.snapshots_of(db, [db.get(models.Purchase, p_c)])
            problems = wallet_reconcile.check(db)["problems"]
        check(L + "a deleted purchase keeps its snapshot; a new purchase with its id inherits nothing; no alert",
              (kept, inherited, problems), (1, [], []))
        with Session() as db:
            table = rt.loyalty_cutover_purchase_reservations
            db.execute(table.update().where(table.c.purchase_id == p_d).values(reserved_quota_bytes=GB))
            db.commit()
            alert = [p["code"] for p in wallet_reconcile.check(db)["problems"]]
        check(L + "a changed snapshot is reported", alert, ["cutover_snapshot_changed"])
        with Session() as db:
            db.execute(table.update().where(table.c.purchase_id == p_d).values(reserved_quota_bytes=20 * GB))
            db.commit()

        # ---------------------------------------------------------------- Void option A
        with Session() as db:
            for name in ("preview", "worker", "execute"):
                flags.set_flag(db, name, True)
        item = env.begin("renew", n_v, 1000, package_id=env.ids.shared, renew_purchase_id=p_v2)
        env.run(lambda db: bot_router.renew_service(n_v, p_v2, schemas.BotRenewRequest(
            add_gb=10, add_days=30, package_id=env.ids.shared, paid_amount=1000, payment_method="card",
            approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=1000),
            db=db, owner_admin_id=None, principal=env.internal))
        shown = env.preview(item["approval_uuid"])
        scope_ids = sorted(p["id"] for p in shown["resources"]["purchases"])
        queued = next(p for p in shown["resources"]["purchases"] if p["id"] == p_v)
        prior = shown["resources"]["prior_rights"]
        check(L + "Void preview of a renewal: the contract's full-account scope, every purchase with its queue",
              (shown["resources"]["delete"]["kind"], scope_ids, queued["reserved_quota_bytes"],
               queued["reserved_max_concurrent_sessions"]), ("user", sorted([p_v, p_v2]), 20 * GB, 3))
        check(L + "...the cutover snapshot of the OTHER purchase, with the note that it is not every historical right",
              ([s["purchase_id"] for s in prior["cutover_snapshots"]], "not a record of every right" in prior["note"]),
              ([p_v], True))
        with Session() as db:
            db.get(models.Purchase, p_v).reserved_duration_days = 60       # the other purchase's queue changes
            db.commit()
        refused = env.void(shown["preview_token"], n_v)
        check(L + "...bound to the confirmation: a change there refuses the void", refused,
              (409, "preview_state_changed"))

        with Session() as db:
            check(L + "the resolution tool is refused after the activation",
                  code(lambda: resolution.decide(db, root, user_id=u_a, action="retire_residual",
                                                 reason="after activation, must be refused")), "activation_active")
        print(f"DONE  {label}")


with tempfile.TemporaryDirectory(prefix="um-resv-transition-") as directory:
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
