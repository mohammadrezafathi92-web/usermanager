"""Changing the concurrent-session cap of ONE purchased service
(services/purchase_cap_repair.py, UserDetail -> «تغییر سقف اتصال هم‌زمان»).
SQLite and real MariaDB (CI). No network.

A customer with four 3-user services: one becomes 5-user, another 1-user.
  * only that Purchase's max_concurrent_sessions changes - not the Package,
    the User, the other Purchases, nor the queued renewal's own cap;
  * RADIUS (enforced) follows it at once, and a PPP credential's own cap (1)
    does not hold back a raised Purchase cap;
  * lowering it: the poller kicks only that service's excess WireGuard
    (newest first); open PPP sessions are not cut, new logins are refused;
  * every change is audited (admin, before, after, time);
  * the value is a whole number 1..1000; nothing else is accepted;
  * two changes at once: one wins, the other is refused (no stale write);
  * refused while other work changes the same service (an open receipt
    approval, a provisioning operation, a void hold, a cancellation);
  * superadmin only; the change re-asks the password.
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
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-purchase-cap-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from fastapi import HTTPException
from pyrad import packet
from pyrad.dictionary import Dictionary

from app import models
from app.deps import require_confirm_password, require_superadmin
from app.routers import session_caps as routes
from app.services import purchase_cap_repair as repair, quota_manager, radius_server, reseller_refund_holds
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


def login(server, username, ip):
    pkt = packet.AuthPacket(id=1, secret=b"testsecret", authenticator=os.urandom(16), dict=_dict)
    pkt["User-Name"] = username
    pkt["User-Password"] = pkt.PwCrypt("pw")
    pkt["Calling-Station-Id"] = ip
    pkt.source = (ip, 12345)
    replies = []
    with patch.object(radius_server, "_send_reply", side_effect=lambda srv, p, reply: replies.append(reply)), \
            patch.object(radius_server, "_notify_ban_async", side_effect=lambda *a, **k: None):
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
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 3})
            user = wallet_accounts.create_user_with_wallet(db, username="fourcaps", telegram_id=7601)
            user.status, user.max_concurrent_sessions = models.UserStatus.active, 3
            db.flush()
            purchases, conns = [], {}
            for n in range(4):
                p = models.Purchase(user_id=user.id, status=models.UserStatus.active, quota_bytes=0,
                                    max_concurrent_sessions=3, package_id=env.ids.shared,
                                    package_name_snapshot=f"three-{n}")
                db.add(p)
                db.flush()
                purchases.append(p.id)
            for n, pid in enumerate(purchases):
                if n == 1:       # the WireGuard service: three peers, all online
                    for k in range(3):
                        c = models.Connection(user_id=user.id, node_id=node, type=models.ConnectionType.wireguard,
                                              enabled=True, purchase_id=pid, max_concurrent_sessions=1, online=True,
                                              wg_peer_name=f"four-wg-{k}")
                        db.add(c)
                        db.flush()
                        conns[f"wg{k}"] = c.id
                else:            # a PPP service: one OpenVPN credential with its own cap of 1
                    c = models.Connection(user_id=user.id, node_id=node, type=models.ConnectionType.openvpn,
                                          enabled=True, purchase_id=pid, max_concurrent_sessions=1,
                                          ppp_username=f"four-ovpn-{n}", ppp_password="pw")
                    db.add(c)
                    db.flush()
                    conns[f"ovpn{n}"] = c.id
            db.commit()
            uid = user.id
        P1, P2, P3, P4 = purchases

        def sessions(key, n):
            with env.Session() as db:
                db.query(models.RadiusActiveSession).filter_by(connection_id=conns[key]).delete()
                for i in range(n):
                    db.add(models.RadiusActiveSession(connection_id=conns[key], session_id=f"{key}-{i}",
                                                      client_ip=f"10.9.0.{i + 1}"))
                db.commit()

        def live(key):
            with env.Session() as db:
                return db.query(models.RadiusActiveSession).filter_by(connection_id=conns[key]).count()

        def unban():
            with env.Session() as db:
                db.query(models.Connection).update({"banned_until": None})
                db.commit()
            server._overlimit_attempts.clear()

        def caps():
            with env.Session() as db:
                return ([db.get(models.Purchase, p).max_concurrent_sessions for p in purchases],
                        db.get(models.User, uid).max_concurrent_sessions,
                        db.get(models.Package, env.ids.shared).max_concurrent_sessions)

        def preview(pid, value, field="max_concurrent_sessions"):
            with env.Session() as db:
                return code(lambda: repair.preview(db, pid, value, field))

        def change(pid, value, shown=None, field="max_concurrent_sessions", admin=None):
            shown = shown or preview(pid, value, field)
            if isinstance(shown, tuple):
                return shown
            return code(lambda: repair.execute(
                env.Session, admin or env.root.id, purchase_id=pid, expected_user_id=shown["user_id"],
                expected_current=shown["current_value"], new_value=value, preview_token=shown["preview_token"],
                reason="owner asked", field=field))

        toggles = []

        def poll():
            toggles.clear()
            fake = lambda db, c, enabled: (toggles.append((c.id, enabled)), setattr(c, "enabled", enabled))
            with patch.object(quota_manager, "_set_connection_enabled", side_effect=fake), \
                    patch.object(quota_manager, "_reconcile_connection_enabled_state",
                                 side_effect=lambda db, now: 0), env.Session() as db:
                quota_manager._finish_cycle(db, None, [], [])
            return sorted(toggles)

        # ============================================================ raise one service to 5
        sessions("ovpn0", 3)
        check(L + "before: service 1 (cap 3) refuses a 4th session on its credential",
              login(server, "four-ovpn-0", "10.9.1.1"), REJECT)
        unban()
        shown = preview(P1, 5)
        with env.Session() as db:
            no_row = db.get(models.SessionCapRuntimeState, 1) is None
        check(L + "no mode row and never changed (as on server 2): the preview reports the effective default",
              (no_row, preview(P1, 5)["session_cap_mode"]), (True, "enforced"))
        check(L + "preview: current 3 -> new 5, enforced now, 3 live PPP sessions, nothing in excess",
              (shown["current_value"], shown["new_value"], shown["in_force_now"], shown["live_ppp_sessions"],
               shown["excess_now"], shown["busy"]), (3, 5, True, 3, 0, None))
        check(L + "preview shows the credential's own cap (1) - which the Purchase cap replaces",
              [r["connection_cap"] for r in shown["scope"]], [1])
        before = dt.datetime.utcnow() - dt.timedelta(seconds=1)
        done = change(P1, 5, shown)
        check(L + "service 1 is now 5-user", (done["before"], done["after"]), (3, 5))
        check(L + "only that Purchase changed: not the other three, the User or the Package",
              caps(), ([5, 3, 3, 3], 3, 3))
        with env.Session() as db:
            audit = db.query(models.PurchaseCapRepair).filter_by(purchase_id=P1).one()
            check(L + "audited: admin, before, after, field, reason, time",
                  (audit.admin_id, audit.before_value, audit.after_value, audit.field, audit.reason,
                   audit.created_at >= before), (env.root.id, 3, 5, "max_concurrent_sessions", "owner asked", True))
        check(L + "RADIUS: a 4th session on the cap-1 credential is accepted now (the credential's cap does not apply)",
              login(server, "four-ovpn-0", "10.9.1.2"), ACCEPT)
        sessions("ovpn0", 5)
        check(L + "RADIUS: at 5 the 6th is refused", login(server, "four-ovpn-0", "10.9.1.3"), REJECT)
        unban()
        sessions("ovpn2", 3)
        check(L + "RADIUS: another service of the same customer keeps its 3", login(server, "four-ovpn-2", "10.9.1.4"),
              REJECT)
        unban()

        # ============================================================ lower one: PPP and WireGuard
        shown = preview(P1, 2)
        check(L + "lowering 5 -> 2 with 5 live: the preview says 3 are in excess, all PPP",
              (shown["excess_now"], shown["live_ppp_sessions"], shown["live_other_sessions"]), (3, 5, 0))
        change(P1, 2, shown)
        check(L + "lowered: open PPP sessions are not cut", live("ovpn0"), 5)
        check(L + "...new logins are refused", login(server, "four-ovpn-0", "10.9.1.5"), REJECT)
        unban()
        check(L + "poller before any change of service 2: nothing to kick", poll(), [])
        change(P2, 1)
        check(L + "service 2 lowered to 1: the poller kicks its two newest peers only",
              poll(), sorted([(conns["wg2"], False), (conns["wg1"], False)]))
        check(L + "...and the other services and the User are as they were", caps(), ([2, 1, 3, 3], 3, 3))

        # ============================================================ values
        for bad in (0, -1, 1001, True, "3", None, 2.5):
            check(L + f"value {bad!r} refused", preview(P3, bad), (422, "purchase_cap_value_invalid"))
        with env.Session() as db:
            row = db.get(models.Purchase, P3)
            row.reserved_package_id, row.reserved_max_concurrent_sessions = env.ids.shared, 4
            row.reserved_created_at = dt.datetime.utcnow()
            db.commit()
        change(P3, 2)
        with env.Session() as db:
            row = db.get(models.Purchase, P3)
            check(L + "a queued renewal's own cap is left alone by a change of the cap in force",
                  (row.max_concurrent_sessions, row.reserved_max_concurrent_sessions), (2, 4))

        # ============================================================ two changes at once
        first, second = preview(P4, 4), preview(P4, 6)
        check(L + "two previews of the same state", first["current_value"] == second["current_value"] == 3)
        results, gate = [], threading.Barrier(2)

        def run(shown, value):
            gate.wait()
            results.append(change(P4, value, shown))
        threads = [threading.Thread(target=run, args=(first, 4)), threading.Thread(target=run, args=(second, 6))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        winners = [r for r in results if isinstance(r, dict)]
        losers = [r for r in results if isinstance(r, tuple)]
        check(L + "concurrent: exactly one change wins, the other is refused - never a stale overwrite",
              (len(winners), [r[0] for r in losers]), (1, [409]))
        check(L + "...the cap is the winner's", caps()[0][3], winners[0]["after"])
        check(L + "...and one audit row for it", _count_audit(env, P4), 1)
        stale = preview(P4, 7)
        with env.Session() as db:
            db.get(models.Purchase, P4).max_concurrent_sessions = 9
            db.commit()
        check(L + "changed after the preview: refused", change(P4, 7, stale), (409, "purchase_cap_target_changed"))

        # ============================================================ stable service / term identity
        with env.Session() as db:
            isolated = models.Purchase(user_id=uid, status=models.UserStatus.active,
                                       max_concurrent_sessions=3)
            db.add(isolated)
            db.commit()
            isolated_id, isolated_key = isolated.id, isolated.history_key
        if engine.dialect.name == "sqlite":
            old_preview = preview(isolated_id, 5)
            with env.Session() as db:
                db.delete(db.get(models.Purchase, isolated_id))
                db.commit()
                replacement = models.Purchase(user_id=uid, status=models.UserStatus.active,
                                              max_concurrent_sessions=3)
                db.add(replacement)
                db.commit()
                check(L + "SQLite reuses the deleted empty service id, but not its identity",
                      (replacement.id == isolated_id, replacement.history_key != isolated_key), (True, True))
            check(L + "old preview cannot change the replacement service",
                  change(isolated_id, 5, old_preview), (409, "purchase_cap_preview_stale"))
            with env.Session() as db:
                check(L + "replacement cap and audit remain untouched",
                      (db.get(models.Purchase, isolated_id).max_concurrent_sessions,
                       db.query(models.PurchaseCapRepair).filter_by(purchase_id=isolated_id).count()), (3, 0))

        # Historical NULL keys are read-only: use created_at as fallback,
        # without assigning a key during preview or failing the SQL CAS.
        with env.Session() as db:
            historical = db.get(models.Purchase, isolated_id)
            historical.history_key = None
            db.commit()
            created = historical.created_at
        historical_preview = preview(isolated_id, 4)
        check(L + "historical service without a key remains editable",
              change(isolated_id, 4, historical_preview)["after"], 4)
        with env.Session() as db:
            row = db.get(models.Purchase, isolated_id)
            check(L + "preview and change do not create a historical key",
                  (row.history_key, row.created_at), (None, created))
        old_preview = preview(isolated_id, 5)
        with env.Session() as db:
            db.get(models.Purchase, isolated_id).created_at = created + dt.timedelta(seconds=1)
            db.commit()
        check(L + "historical creation identity changed: old preview refused",
              change(isolated_id, 5, old_preview), (409, "purchase_cap_preview_stale"))

        for seq in ("reservation_activation_seq", "renewal_event_seq"):
            old_preview = preview(isolated_id, 5)
            with env.Session() as db:
                row = db.get(models.Purchase, isolated_id)
                setattr(row, seq, int(getattr(row, seq) or 0) + 1)
                db.commit()
            check(L + f"{seq} changed while cap/scope stayed the same: old preview refused",
                  change(isolated_id, 5, old_preview), (409, "purchase_cap_preview_stale"))
        old_preview = preview(isolated_id, 5)
        with env.Session() as db:
            db.get(models.Purchase, isolated_id).used_bytes = 123
            db.commit()
        check(L + "ordinary usage does not stale a valid service preview",
              change(isolated_id, 5, old_preview)["after"], 5)
        with env.Session() as db:
            db.get(models.Purchase, isolated_id).created_at = None
            db.commit()
        check(L + "no stable identity at all: refuse rather than bind a numeric id",
              preview(isolated_id, 6), (409, "purchase_cap_identity_unavailable"))

        # ============================================================ other work on the same service
        env.new_user("solo", 7602)
        with env.Session() as db:
            solo = db.query(models.Purchase).join(models.User).filter(models.User.username == "solo").one().id
        item = env.begin("renew", "solo", 1000, package_id=env.ids.shared)
        check(L + "renewal approval registered for the service", item.get("state"), "registered")
        check(L + "open receipt approval on the service: preview says so, change refused",
              (preview(solo, 2)["busy"], change(solo, 2)), ("purchase_cap_approval_in_progress",
                                                         (409, "purchase_cap_approval_in_progress")))
        for name, code_ in (("_operation_open", "purchase_cap_operation_in_progress"),
                            ("_payment_hold", "purchase_cap_operation_in_progress"),
                            ("_void_held", "purchase_cap_void_in_progress")):
            with patch.object(reseller_refund_holds, name, lambda *a, **k: True):
                check(L + f"{name.strip('_')}: refused", change(P3, 1), (409, code_))
        check(L + "nothing changed by the refusals", caps()[0][2], 2)

        # ============================================================ who may
        with env.Session() as db:
            reseller = db.get(models.AdminUser, env.ids.reseller)
            check(L + "a reseller is not a superadmin: refused before anything runs",
                  code(lambda: require_superadmin(reseller))[0], 403)

        with env.Session() as db:
            mode.change_mode(env.Session, env.root.id, "legacy", "checking the legacy preview")
        shown = preview(P3, 3)
        check(L + "in legacy the preview says the cap is not in force",
              (shown["session_cap_mode"], shown["in_force_now"]), ("legacy", False))
        with env.Session() as db:
            db.get(models.SessionCapRuntimeState, 1).mode = "bogus"
            db.commit()
        shown = preview(P3, 3)
        check(L + "an unreadable mode (RADIUS/poller decide nothing): 'unavailable', not in force",
              (shown["session_cap_mode"], shown["in_force_now"]), ("unavailable", False))
        sessions("ovpn3", 0)
        check(L + "...and RADIUS then answers no login at all - not accepted, the cap is never bypassed",
              login(server, "four-ovpn-3", "10.9.2.1"), None)


def _count_audit(env, pid):
    with env.Session() as db:
        return db.query(models.PurchaseCapRepair).filter_by(purchase_id=pid).count()


def routes_guarded():
    from pydantic import ValidationError
    for invalid in (True, 2.5, "3"):
        for request, extra in ((routes.RepairPreviewRequest, {}),
                               (routes.RepairExecuteRequest, dict(expected_user_id=1, preview_token="t", reason="test"))):
            try:
                request(purchase_id=1, new_value=invalid, **extra)
                rejected = False
            except ValidationError:
                rejected = True
            check(f"HTTP schema {request.__name__} refuses non-integer {invalid!r}", rejected)
    by_path = {(r.path, tuple(sorted(r.methods))): r for r in routes.router.routes}
    check("routes: superadmin-only",
          all(any(d.call is require_superadmin for d in r.dependant.dependencies) for r in by_path.values()))
    check("routes: the change re-asks the password, the preview does not",
          {p: any(d.call is require_confirm_password for d in r.dependant.dependencies)
           for (p, m), r in by_path.items() if "purchase-repairs" in p and "POST" in m},
          {"/api/session-caps/purchase-repairs/preview": False, "/api/session-caps/purchase-repairs/execute": True})


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="purchase_cap_change_"))
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
