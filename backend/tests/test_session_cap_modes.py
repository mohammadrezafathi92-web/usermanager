"""session_cap_mode: the staged rollout of the per-Purchase session cap, on
SQLite AND real MariaDB (CI). No network.

  legacy    the default after deploy (no state row): RADIUS, the poller,
            bans and ban messages exactly as before
  shadow    legacy decides and acts; the disagreement is recorded, nothing
            else - no reject, kick, ban or message of its own
  enforced  the per-Purchase rule
  rollback  enforced -> legacy: the legacy poller releases what enforced
            kicked, by its own rule
  switch    a change waits for a decision in flight; a decision started
            during a change waits and then uses the new mode; a change
            refused for a busy lock leaves the mode as it was
  repair    preview then execute, compare-and-set on Purchase, User,
            current value and the scope's connections; audited
  routes    superadmin-only; the two writes re-ask the password
"""
import contextlib
import os
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-session-cap-mode-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from pyrad import packet
from pyrad.dictionary import Dictionary

from app import models
from app.deps import require_confirm_password, require_superadmin
from app.routers import session_caps as routes
from app.services import gate_locks, purchase_cap_repair as repair, quota_manager, radius_server
from app.services import session_cap_mode as mode
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def code(fn):
    try:
        return fn()
    except HTTPException as exc:
        return exc.status_code, exc.detail


_dict = Dictionary(radius_server.DICT_PATH)
ACCEPT, REJECT = packet.AccessAccept, packet.AccessReject
NOTIFIED = []


def login(server, username, ip):
    pkt = packet.AuthPacket(id=1, secret=b"testsecret", authenticator=os.urandom(16), dict=_dict)
    pkt["User-Name"] = username
    pkt["User-Password"] = pkt.PwCrypt("pw")
    pkt["Calling-Station-Id"] = ip
    pkt.source = (ip, 12345)
    replies = []
    with patch.object(radius_server, "_send_reply", side_effect=lambda srv, p, reply: replies.append(reply)), \
            patch.object(radius_server, "_notify_ban_async", side_effect=lambda *a, **k: NOTIFIED.append(a)):
        server.HandleAuthPacket(pkt)
    return replies[-1].code if replies else None


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        node = env.ids.node
        radius_server.SessionLocal = env.Session
        server = object.__new__(radius_server.UserManagerRadiusServer)
        server._rejection_log_times = {}
        server._overlimit_attempts = {}
        mode._shadow.clear()

        with env.Session() as db:
            from app.services import wallet_accounts
            user = wallet_accounts.create_user_with_wallet(db, username="modeuser", telegram_id=7501)
            user.status = models.UserStatus.active
            db.flush()
            purchase = models.Purchase(user_id=user.id, status=models.UserStatus.active, quota_bytes=0,
                                       max_concurrent_sessions=1, package_name_snapshot="cap-1")
            db.add(purchase)
            db.flush()

            def conn(kind, name=None, **extra):
                row = models.Connection(user_id=user.id, node_id=node, type=kind, enabled=True, purchase_id=purchase.id,
                                        ppp_username=name, ppp_password="pw" if name else None,
                                        max_concurrent_sessions=1, wg_peer_name=None if name else "mode-wg", **extra)
                db.add(row)
                db.flush()
                return row.id
            ids = dict(ovpn=conn(models.ConnectionType.openvpn, "mode-ovpn"),
                       l2tp=conn(models.ConnectionType.l2tp, "mode-l2tp"),
                       wg=conn(models.ConnectionType.wireguard, online=True))
            db.commit()
            uid, pid = user.id, purchase.id

        def sessions(key, n):
            with env.Session() as db:
                db.query(models.RadiusActiveSession).filter_by(connection_id=ids[key]).delete()
                for i in range(n):
                    db.add(models.RadiusActiveSession(connection_id=ids[key], session_id=f"{key}-{i}",
                                                      client_ip=f"10.7.0.{i + 1}"))
                db.commit()

        def events():
            with env.Session() as db:
                return db.query(models.RadiusLimitEventLog).filter_by(user_id=uid).count()

        def shadow_rows():
            with env.Session() as db:
                return sorted((r.kind, r.connection_id, r.occurrences) for r in db.query(models.SessionCapShadowEvent))

        toggles = []

        def poll():
            toggles.clear()
            fake = lambda db, c, enabled: (toggles.append((c.id, enabled)), setattr(c, "enabled", enabled))
            with patch.object(quota_manager, "_set_connection_enabled", side_effect=fake), env.Session() as db:
                quota_manager.enforce_concurrent_session_limits(db)
                db.commit()
                wg = db.get(models.Connection, ids["wg"])
                return sorted(toggles), (wg.enabled, wg.session_limited)

        def unban():
            with env.Session() as db:
                db.query(models.Connection).update({"banned_until": None})
                db.commit()
            server._overlimit_attempts.clear()

        # ================================================================ legacy (default)
        with env.Session() as db:
            check(L + "after deploy: no state row, mode legacy", (db.get(models.SessionCapRuntimeState, 1),
                                                                 mode.status(db)["mode"]), (None, "legacy"))
        sessions("ovpn", 1)
        check(L + "legacy: a 2nd protocol of a cap-1 Purchase is accepted (each connection's own cap), no event",
              (login(server, "mode-l2tp", "10.8.0.1"), events(), dict(mode._shadow)), (ACCEPT, 0, {}))
        check(L + "legacy: the poller kicks nothing for a customer without a User cap", poll(), ([], (True, False)))

        def ban_trail():
            """A second login on the busy OpenVPN credential, repeated until the
            legacy over-limit ban: replies, logged events, the ban, messages."""
            unban()
            NOTIFIED.clear()
            before = events()
            attempts = radius_server.OVERLIMIT_ATTEMPTS_THRESHOLD + 1
            replies = [login(server, "mode-ovpn", f"10.8.1.{i}") for i in range(1, attempts + 1)]
            with env.Session() as db:
                banned = db.get(models.Connection, ids["ovpn"]).banned_until is not None
            return replies, events() - before, banned, len(NOTIFIED)

        legacy_trail = ban_trail()
        check(L + "legacy: the over-limit credential is refused, logged, banned and messaged as before",
              (legacy_trail[0][0], legacy_trail[1] > 0, legacy_trail[2], legacy_trail[3] > 0), (REJECT, True, True, True))

        # ================================================================ shadow
        check(L + "change to shadow (audited)", code(lambda: mode.change_mode(
            env.Session, env.root.id, "shadow", "measure first"))["mode"], "shadow")
        check(L + "shadow: refusals, logged events, ban and ban messages identical to legacy", ban_trail(),
              legacy_trail)
        unban()
        mode._shadow.clear()
        with env.Session() as db:
            db.query(models.SessionCapShadowEvent).delete()
            db.commit()
        before = events()
        replies = [login(server, "mode-l2tp", f"10.8.2.{i}") for i in range(1, 4)]
        check(L + "shadow: the 2nd protocol is still accepted (legacy acts), no event, no message",
              (replies, events() - before, len(NOTIFIED) - legacy_trail[3]), ([ACCEPT] * 3, 0, 0))
        check(L + "shadow: the poller still kicks nothing", poll(), ([], (True, False)))
        mode.flush_shadow(env.Session)
        check(L + "shadow: the disagreements are recorded once per 5 minutes and kind",
              shadow_rows(), sorted([("radius_would_reject", ids["l2tp"], 3), ("poll_would_kick", ids["wg"], 1)]))

        # ================================================================ enforced, then rollback
        mode.change_mode(env.Session, env.root.id, "enforced", "owner decided")
        unban()
        check(L + "enforced: the 2nd protocol of the cap-1 Purchase is refused",
              login(server, "mode-l2tp", "10.8.3.1"), REJECT)
        check(L + "enforced: the poller kicks the Purchase's WireGuard", poll(), ([(ids["wg"], False)], (False, True)))
        mode.change_mode(env.Session, env.root.id, "legacy", "rollback")
        check(L + "rollback to legacy: the legacy poller releases the kick (no User cap)", poll(),
              ([(ids["wg"], True)], (True, False)))
        with env.Session() as db:
            db.get(models.User, uid).max_concurrent_sessions = 1
            db.commit()
        mode.change_mode(env.Session, env.root.id, "enforced", "again")
        poll()
        mode.change_mode(env.Session, env.root.id, "legacy", "rollback 2")
        check(L + "rollback with a User cap of 1 and the OpenVPN still on: legacy keeps it kicked (no room)",
              poll(), ([], (False, True)))
        sessions("ovpn", 0)
        check(L + "...released by legacy once the OpenVPN session ends", poll(), ([(ids["wg"], True)], (True, False)))
        with env.Session() as db:
            db.get(models.User, uid).max_concurrent_sessions = None
            history = [(e.from_mode, e.to_mode) for e in db.query(models.SessionCapModeEvent).order_by(
                models.SessionCapModeEvent.id)]
        check(L + "every mode change audited", history, [("legacy", "shadow"), ("shadow", "enforced"),
                                                          ("enforced", "legacy"), ("legacy", "enforced"),
                                                          ("enforced", "legacy")])

        # ================================================================ the switch
        bind = env.Session().get_bind()
        held, release, outcome = threading.Event(), threading.Event(), {}

        def in_flight():
            with mode.decision(bind) as seen:
                outcome["in_flight"] = seen
                held.set()
                release.wait(10)

        def change(target):
            outcome[target] = code(lambda: mode.change_mode(env.Session, env.root.id, target, "switch test"))

        reader = threading.Thread(target=in_flight)
        reader.start()
        held.wait(5)
        changer = threading.Thread(target=change, args=("enforced",))
        changer.start()
        time.sleep(0.5)
        with env.Session() as db:
            pending = (changer.is_alive(), mode.status(db)["mode"])
        release.set()
        reader.join(10)
        changer.join(10)
        with mode.decision(bind) as after:
            pass
        check(L + "a change waits for the decision in flight (still legacy), then answers; the next decision is "
              "enforced", (pending, outcome["in_flight"], outcome["enforced"]["mode"], after),
              ((True, "legacy"), "legacy", "enforced", "enforced"))

        exclusive = gate_locks.FileLock(mode.lock_path()).acquire(shared=False, timeout=5)
        with env.Session() as db:   # what a change does while it holds the lock: commit first
            db.query(models.SessionCapRuntimeState).update({"mode": "legacy"})
            db.commit()
        seen = {}

        def wait_for_decision():
            with mode.decision(bind) as decided:
                seen["mode"] = decided

        waiter = threading.Thread(target=wait_for_decision)
        started = time.monotonic()
        waiter.start()
        time.sleep(0.3)
        blocked = waiter.is_alive()
        exclusive.release()
        waiter.join(10)
        check(L + "a decision started during a change waits for it, then uses the new mode",
              (blocked, seen.get("mode"), time.monotonic() - started < mode.READ_WAIT_SECONDS + 1), (True, "legacy", True))

        holder = mode.decision(bind)
        holder.__enter__()
        busy = code(lambda: mode.change_mode(env.Session, env.root.id, "shadow", "busy", timeout=0.2))
        holder.__exit__(None, None, None)
        with env.Session() as db:
            check(L + "a change refused for a busy lock leaves the mode as it was",
                  (busy, mode.status(db)["mode"]), ((409, "session_cap_mode_busy"), "legacy"))
        check(L + "invalid mode / no reason refused", (
            code(lambda: mode.change_mode(env.Session, env.root.id, "off", "x")),
            code(lambda: mode.change_mode(env.Session, env.root.id, "shadow", " "))),
            ((422, "session_cap_mode_invalid"), (422, "session_cap_mode_reason_required")))

        # ================================================================ repair
        with env.Session() as db:
            target = models.Purchase(user_id=uid, status=models.UserStatus.active, quota_bytes=0,
                                     package_name_snapshot="no-cap")
            db.add(target)
            db.flush()
            db.add(models.Connection(user_id=uid, node_id=node, type=models.ConnectionType.openvpn, enabled=True,
                                     purchase_id=target.id, ppp_username="repair-ovpn", ppp_password="pw",
                                     max_concurrent_sessions=1))
            db.commit()
            tid = target.id

        def preview(value=3):
            with env.Session() as db:
                return repair.preview(db, tid, value)

        def execute(shown, **over):
            args = dict(purchase_id=tid, expected_user_id=uid, expected_current=shown["current_value"],
                        new_value=shown["new_value"], preview_token=shown["preview_token"], reason="sibling evidence")
            args.update(over)
            return code(lambda: repair.execute(env.Session, env.root.id, **args))

        shown = preview()
        check(L + "preview: the Purchase, its scope, no effect under legacy",
              (shown["current_value"], [r["type"] for r in shown["scope"]], shown["effect"]["legacy_and_shadow"][:4]),
              (None, ["openvpn"], "none"))
        with env.Session() as db:
            unchanged = db.get(models.Purchase, tid).max_concurrent_sessions
        check(L + "preview writes nothing", unchanged, None)
        with env.Session() as db:
            db.add(models.Connection(user_id=uid, node_id=node, type=models.ConnectionType.l2tp, enabled=True,
                                     purchase_id=tid, ppp_username="repair-l2tp", ppp_password="pw"))
            db.commit()
        check(L + "a connection added to the scope since the preview: refused", execute(shown),
              (409, "purchase_cap_preview_stale"))
        shown = preview()
        with env.Session() as db:
            db.get(models.User, uid).max_concurrent_sessions = 2
            db.commit()
        check(L + "the User's cap changed since the preview: refused", execute(shown),
              (409, "purchase_cap_preview_stale"))
        shown = preview()
        check(L + "wrong customer / wrong current value: refused",
              (execute(shown, expected_user_id=uid + 999), execute(shown, expected_current=5)),
              ((409, "purchase_cap_target_changed"), (409, "purchase_cap_target_changed")))
        check(L + "invalid value / no reason: refused",
              (code(lambda: preview(0)), execute(shown, reason="  ")),
              ((422, "purchase_cap_value_invalid"), (422, "purchase_cap_reason_required")))
        done = execute(shown)
        with env.Session() as db:
            audit = [(a.purchase_id, a.before_value, a.after_value, a.user_cap_at_repair, a.reason, a.admin_id)
                     for a in db.query(models.PurchaseCapRepair)]
            now_cap = db.get(models.Purchase, tid).max_concurrent_sessions
        check(L + "execute: compare-and-set applied once, audited",
              (done["before"], done["after"], now_cap, audit), (None, 3, 3, [(tid, None, 3, 2, "sibling evidence",
                                                                              env.root.id)]))
        check(L + "the same preview again: refused (the value is no longer the previewed one)", execute(shown),
              (409, "purchase_cap_target_changed"))


def routes_guarded():
    by_path = {(r.path, tuple(sorted(r.methods))): r for r in routes.router.routes}
    superadmin = all(any(d.call is require_superadmin for d in r.dependant.dependencies) for r in by_path.values())
    confirm = {path: any(d.call is require_confirm_password for d in r.dependant.dependencies)
               for (path, methods), r in by_path.items() if "POST" in methods}
    check("routes: every route superadmin-only", superadmin, True)
    check("routes: the mode change and the repair execute re-ask the password; the preview writes nothing and "
          "does not", confirm, {"/api/session-caps/mode": True, "/api/session-caps/purchase-repairs/preview": False,
                                "/api/session-caps/purchase-repairs/execute": True})


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="session_cap_modes_"))
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
    routes_guarded()

check("no network", _no_network.attempts, [])
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
