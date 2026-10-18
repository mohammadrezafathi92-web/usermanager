"""session_cap_backfill: at every startup the per-Purchase caps are filled
from the linked packages, so the default session_cap_mode 'enforced' needs
no command. SQLite AND real MariaDB (CI). No network.

  empty Purchase cap, linked package with a cap   -> that cap, audited
  empty queued-renewal cap                         -> the renewal package's
  no linked package / package without a cap       -> left empty
  a cap already set                                -> never changed
  RADIUS PPP connections of a capped Purchase      -> raised to its cap,
                                                      never lowered; WG/Xray
                                                      untouched
  a second run                                     -> changes nothing
"""
import contextlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-session-cap-backfill-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from app import models
from app.services import session_cap_backfill, wallet_accounts
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
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        with env.Session() as db:
            three = models.Package(name="three", quota_gb=1, duration_days=30, price=1, max_concurrent_sessions=3)
            uncapped = models.Package(name="uncapped", quota_gb=1, duration_days=30, price=1, max_concurrent_sessions=None)
            db.add_all([three, uncapped])
            db.flush()
            user = wallet_accounts.create_user_with_wallet(db, username="backfill", telegram_id=7601)
            db.flush()

            def purchase(**kw):
                row = models.Purchase(user_id=user.id, status=models.UserStatus.active, quota_bytes=0, **kw)
                db.add(row)
                db.flush()
                return row

            def conn(p, kind, cap):
                row = models.Connection(user_id=user.id, node_id=env.ids.node, type=kind, enabled=True,
                                        purchase_id=p.id, max_concurrent_sessions=cap,
                                        ppp_username=f"bf{p.id}{kind.value}" if kind != models.ConnectionType.wireguard else None,
                                        wg_peer_name=f"bf{p.id}" if kind == models.ConnectionType.wireguard else None)
                db.add(row)
                db.flush()
                return row.id

            empty = purchase(package_id=three.id)
            ids = dict(ovpn=conn(empty, models.ConnectionType.openvpn, 1),
                       l2tp=conn(empty, models.ConnectionType.l2tp, 5),
                       wg=conn(empty, models.ConnectionType.wireguard, 1))
            queued = purchase(package_id=uncapped.id, reserved_package_id=three.id, reserved_quota_bytes=1)
            nopkg = purchase()
            nocap = purchase(package_id=uncapped.id)
            already = purchase(package_id=three.id, max_concurrent_sessions=2)
            db.commit()
            pids = dict(empty=empty.id, queued=queued.id, nopkg=nopkg.id, nocap=nocap.id, already=already.id)

        first = session_cap_backfill.run(env.Session)
        with env.Session() as db:
            got = {k: (db.get(models.Purchase, v).max_concurrent_sessions,
                       db.get(models.Purchase, v).reserved_max_concurrent_sessions) for k, v in pids.items()}
            conns = {k: db.get(models.Connection, v).max_concurrent_sessions for k, v in ids.items()}
            audit = sorted((a.purchase_id, a.field, a.before_value, a.after_value, a.admin_id)
                           for a in db.query(models.PurchaseCapRepair))
        check(L + "purchase caps filled from the linked package only where empty",
              got, dict(empty=(3, None), queued=(None, 3), nopkg=(None, None), nocap=(None, None), already=(2, None)))
        check(L + "PPP connection caps raised to the Purchase's, never lowered; WireGuard untouched",
              conns, dict(ovpn=3, l2tp=5, wg=1))
        check(L + "each filled Purchase field audited (system, admin 0)", audit, sorted([
            (pids["empty"], "max_concurrent_sessions", None, 3, 0),
            (pids["queued"], "reserved_max_concurrent_sessions", None, 3, 0)]))
        check(L + "counts reported", first, dict(purchase_caps=1, reserved_caps=1, connection_caps=1))
        check(L + "a second run changes nothing", session_cap_backfill.run(env.Session),
              dict(purchase_caps=0, reserved_caps=0, connection_caps=0))


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="session_cap_backfill_"))
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
