"""Every Purchase has its OWN concurrent-session cap (owner, 2026-10-17):
never summed across a customer's purchases, never bounded by another one or
by the User. SQLite AND real MariaDB (CI). Fake runner; no network.

Reported 2026-10-17: a customer of a 3-user package could not log in with
three devices - RADIUS read only User.max_concurrent_sessions (empty) and
then each connection's own cap (1); Purchase.max_concurrent_sessions was
stored but enforced nowhere, and the bot's create left it empty.

  durable create_user      the Purchase gets the cap FROZEN with the request,
                           even if the package changes before T_final; the
                           User's cap is not touched
  legacy bot create_user   the absorbed Purchase gets the package cap, not
                           the User's
  renewal with a package  the Purchase gets the renewed package's cap,
                           frozen at the renewal: now if it applies now,
                           at activation if queued; none, or a renewal
                           without a package, leaves it
  approval, no connection  no Purchase: the User's pool holds
                           the package's cap from the approval snapshot
  two purchases, caps 1/3  (session_cap_mode 'enforced') RADIUS: each Purchase admits up to its own cap and
                           refuses the next; a full one never blocks the
                           other; a User cap does not bound either
  fallbacks                Purchase without a cap: the connection's own cap;
                           no Purchase: the User's cap over the legacy
                           connections, else the connection's own
  poller                   a WireGuard connection is kicked only when ITS
                           Purchase is over cap, released when that Purchase
                           has room again, never flaps, never touches the
                           other Purchase
"""
import contextlib
import os
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ["UM_LOCK_DIR"] = tempfile.mkdtemp(prefix="um-session-cap-locks-")
import _mariadb_scratch as scratch
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])

from pyrad import packet
from pyrad.dictionary import Dictionary
from sqlalchemy import select

from app import models, models_receipt_void as rv, schemas
from app.routers import bot as bot_router
from app.services import provisioning_parent_execute as execute, radius_server, remote_action as action
from app.services import quota_manager, session_cap_mode, user_ops
import _receipt_void_env as fixture

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


_dict = Dictionary(radius_server.DICT_PATH)


def login(server, username, password, ip):
    """One PAP Access-Request through the real HandleAuthPacket; the reply code."""
    pkt = packet.AuthPacket(id=1, secret=b"testsecret", authenticator=os.urandom(16), dict=_dict)
    pkt["User-Name"] = username
    pkt["User-Password"] = pkt.PwCrypt(password)
    pkt["Calling-Station-Id"] = ip
    pkt.source = (ip, 12345)
    replies = []
    with patch.object(radius_server, "_send_reply", side_effect=lambda srv, p, reply: replies.append(reply)):
        server.HandleAuthPacket(pkt)
    return replies[-1].code if replies else None


def scenario(label, engine):
    L = f"[{label}] "
    with contextlib.ExitStack() as stack:
        env = fixture.environment(label, engine, stack)
        _NODE[0] = env.ids.node
        with env.Session() as db:
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 3})
            free = models.Package(name="NoConn", quota_gb=5, duration_days=30, price=700, max_concurrent_sessions=2)
            db.add(free)
            db.commit()
            free_id = free.id

        # ======================================================== durable create_user (bot, 'required')
        frozen = {"changed": False}

        def change_package_mid_flight(dto):
            if dto.action_type is action.ActionType.WG_ENSURE_PRESENT and not frozen["changed"]:
                with env.Session() as db:            # the package is edited after T1, before T_final
                    db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 9})
                    db.commit()
                frozen["changed"] = True
            return fixture.runner(dto)

        with patch.object(execute.remote_runner, "run_action", side_effect=change_package_mid_flight):
            env.new_user("capdurable", 7401)
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="capdurable").one()
            purchase = db.query(models.Purchase).filter_by(user_id=user.id).one()
            caps = (purchase.max_concurrent_sessions, user.max_concurrent_sessions, frozen["changed"])
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 3})
            db.commit()
        check(L + "durable create_user: the Purchase gets the cap frozen with the request (3, not the edited 9); "
              "the User's stays empty", caps, (3, None, True))

        # ======================================================== renewal with another package (durable approval)
        env.new_user("caprenew", 7404)
        with env.Session() as db:
            renewed = db.query(models.Purchase).join(models.User).filter(models.User.username == "caprenew").one()
            renewed.max_concurrent_sessions = 1          # a 1-user service, renewed with the 3-user package
            db.commit()
            renewed_id = renewed.id
        env.renew("caprenew")
        with env.Session() as db:
            row = db.get(models.Purchase, renewed_id)
            queued = (row.max_concurrent_sessions, row.reserved_max_concurrent_sessions, row.reserved_package_id)
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 9})
            db.commit()
        with env.Session() as db:
            row = db.get(models.Purchase, renewed_id)
            user_ops._maybe_activate_reserved_purchase_renewal(db, row)
            db.commit()
            activated = (row.max_concurrent_sessions, row.reserved_max_concurrent_sessions)
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 3})
            db.commit()
        check(L + "renewal of a 1-user service with a 3-user package: queued with the cap frozen (3), the service "
              "still 1 until it activates", queued, (1, 3, env.ids.shared))
        check(L + "...activation applies the frozen 3, not the package's edited 9", activated, (3, None))

        # ======================================================== approval with no connection (zero_final)
        item = env.begin("new", "capzero", 700, package_id=free_id, claimed_telegram_id=7402)
        env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
            username="capzero", telegram_id=7402, package_id=free_id, paid_amount=700, payment_method="card",
            approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=700),
            db=db, principal=env.internal))
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="capzero").one_or_none()
            state = env.approval(item["approval_uuid"])["state"]
        check(L + "approval without a connection: no Purchase, the User's pool gets the package cap (2)",
              (state, user.max_concurrent_sessions if user else None), ("completed", 2))

        # ======================================================== legacy bot create_user (registration off)
        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="off", loyalty_timing_mode="legacy_activation", completeness_generation=0))
            for kind in ("create_user", "purchase", "add_connection"):
                db.execute(text_update(kind))
            db.commit()

        def fake_provision(db, user, node, protocol, flow="", *_args, **kwargs):
            connection = models.Connection(user_id=user.id, node_id=node.id, type=protocol, enabled=True,
                                           purchase_batch=kwargs.get("purchase_batch"),
                                           wg_peer_name=f"legacy-{user.id}-{node.id}")
            db.add(connection)
            db.flush()
            return connection

        with patch.object(user_ops, "provision_connection", side_effect=fake_provision):
            outcome = env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
                username="caplegacy", telegram_id=7403, package_id=env.ids.shared,
                connections=[schemas.BotCreateConnectionSpec(node_id=env.ids.node, protocol="wireguard")],
                paid_amount=1000, payment_method="card"), db=db, principal=env.internal))
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="caplegacy").one_or_none()
            purchases = [] if user is None else [p.max_concurrent_sessions for p in
                                                 db.query(models.Purchase).filter_by(user_id=user.id)]
        check(L + "legacy bot create_user: the absorbed Purchase gets the package cap (3), the User's stays empty",
              (getattr(outcome, "username", outcome), purchases, user.max_concurrent_sessions if user else None),
              ("caplegacy", [3], None))

        # ======================================================== legacy renewals
        def legacy_renew(purchase_id, **request):
            with env.Session() as db:
                name = db.get(models.User, db.get(models.Purchase, purchase_id).user_id).username
            return env.run(lambda db: bot_router.renew_service(name, purchase_id, schemas.BotRenewRequest(
                paid_amount=700, payment_method="card", **request), db=db, owner_admin_id=None, principal=env.internal))

        def cap_of(purchase_id):
            with env.Session() as db:
                row = db.get(models.Purchase, purchase_id)
                return row.max_concurrent_sessions, row.reserved_max_concurrent_sessions

        with env.Session() as db:
            row = db.query(models.Purchase).join(models.User).filter(models.User.username == "caplegacy").one()
            row.max_concurrent_sessions, row.used_bytes, row.quota_bytes = 1, 10, 10
            row.status = models.UserStatus.quota_exceeded       # exhausted: the renewal applies now
            db.commit()
            exhausted = row.id
        legacy_renew(exhausted, add_gb=5, add_days=30, package_id=free_id)
        check(L + "legacy renewal of an exhausted 1-user service with a 2-user package: the cap is 2 now",
              cap_of(exhausted), (2, None))
        legacy_renew(exhausted, add_gb=1, add_days=0)
        check(L + "...a renewal without a package leaves the cap", cap_of(exhausted)[0], 2)
        with env.Session() as db:
            row = db.get(models.Purchase, exhausted)
            row.reserved_quota_bytes, row.reserved_package_id = 1024, free_id   # queued before this column existed
            row.max_concurrent_sessions = 1
            user_ops._maybe_activate_reserved_purchase_renewal(db, row)
            db.commit()
        check(L + "...a reservation without a frozen cap activates and leaves the cap as it is", cap_of(exhausted),
              (1, None))

        # ======================================================== two purchases, caps 1 and 3
        # The per-Purchase rule decides only in session_cap_mode 'enforced'
        # (legacy and shadow: test_session_cap_modes.py).
        session_cap_mode.change_mode(env.Session, env.root.id, "enforced", "per-Purchase rule test")
        radius_server.SessionLocal = env.Session
        server = object.__new__(radius_server.UserManagerRadiusServer)
        server._rejection_log_times = {}
        server._overlimit_attempts = {}             # what __init__ sets (no socket is bound here)
        accept, reject = packet.AccessAccept, packet.AccessReject
        with env.Session() as db:
            owner = wallet_user(db, "twocaps")
            one = service(db, owner, 1)
            three = service(db, owner, 3)
            nocap = service(db, owner, None)
            ids = {
                "ovpn1": ppp(db, owner, one, "one-ovpn"), "ovpn3": ppp(db, owner, three, "three-ovpn"),
                "l2tp3": ppp(db, owner, three, "three-l2tp", models.ConnectionType.l2tp),
                "wg1": wg(db, owner, one, "one-wg"), "wg3": wg(db, owner, three, "three-wg"),
                "nocap": ppp(db, owner, nocap, "nocap-ovpn", cap=2),
            }
            legacy_owner = wallet_user(db, "legacycap")
            legacy_owner.max_concurrent_sessions = 2
            ids["legA"] = ppp(db, legacy_owner, None, "leg-a")
            ids["legB"] = ppp(db, legacy_owner, None, "leg-b", models.ConnectionType.l2tp)
            plain = wallet_user(db, "nocapuser")
            ids["single"] = ppp(db, plain, None, "single-ovpn")
            db.commit()
            owner_id = owner.id

        def sessions(**counts):
            with env.Session() as db:
                for key, n in counts.items():
                    db.query(models.RadiusActiveSession).filter_by(connection_id=ids[key]).delete()
                    for i in range(n):
                        db.add(models.RadiusActiveSession(connection_id=ids[key], session_id=f"{key}-{i}",
                                                          client_ip=f"10.9.{len(key)}.{i + 1}"))
                db.commit()

        def logins(name, password_key, before_each):
            results = []
            for n in before_each:
                sessions(**{password_key: n})
                results.append(login(server, name, "pw", f"10.8.0.{n + 1}"))
            return results

        check(L + "RADIUS, Purchase cap 3: logins 1-3 accepted, the 4th refused (one credential)",
              logins("three-ovpn", "ovpn3", (0, 1, 2, 3)), [accept, accept, accept, reject])
        check(L + "RADIUS, Purchase cap 1: the 1st accepted, the 2nd refused",
              logins("one-ovpn", "ovpn1", (0, 1)), [accept, reject])
        sessions(ovpn3=2, l2tp3=1, ovpn1=0)
        check(L + "...cap 3 is counted across that Purchase's connections: 2 OpenVPN + 1 L2TP refuse a 4th",
              login(server, "three-ovpn", "pw", "10.8.1.1"), reject)
        check(L + "...and the full Purchase-3 never blocks Purchase-1", login(server, "one-ovpn", "pw", "10.8.1.2"),
              accept)
        sessions(ovpn3=0, l2tp3=0, ovpn1=1)
        check(L + "...nor the full Purchase-1 Purchase-3", login(server, "three-ovpn", "pw", "10.8.1.3"), accept)
        with env.Session() as db:
            db.get(models.User, owner_id).max_concurrent_sessions = 1
            db.commit()
        check(L + "a User cap of 1 does not bound a Purchase with its own cap (2 open on cap 3 -> accepted)",
              logins("three-ovpn", "ovpn3", (2,)), [accept])
        check(L + "fallback, Purchase without a cap: the connection's own cap (2) - 2 accepted, the 3rd refused",
              logins("nocap-ovpn", "nocap", (0, 1, 2)), [accept, accept, reject])
        sessions(legA=1, legB=0)
        check(L + "fallback, no Purchase: the User's cap (2) over the legacy connections combined",
              (login(server, "leg-b", "pw", "10.8.2.1"), sessions(legB=1),
               login(server, "leg-a", "pw", "10.8.2.2")), (accept, None, reject))
        check(L + "fallback, no Purchase and no User cap: the connection's own 1 refuses a 2nd login",
              logins("single-ovpn", "single", (0, 1)), [accept, reject])
        sessions(ovpn1=0, ovpn3=0, l2tp3=0, nocap=0, legA=0, legB=0, single=0)

        # ======================================================== poller kick / release per Purchase
        toggles = []

        def fake_enabled(db, connection, enabled):
            toggles.append((connection.id, enabled))
            connection.enabled = enabled

        def poll():
            toggles.clear()
            with patch.object(quota_manager, "_set_connection_enabled", side_effect=fake_enabled), \
                    env.Session() as db:
                quota_manager.enforce_concurrent_session_limits(db)
                db.commit()
                state = {k: (c.enabled, c.session_limited) for k, c in
                         ((k, db.get(models.Connection, ids[k])) for k in ("wg1", "wg3"))}
            return sorted(toggles), state

        with env.Session() as db:
            for key in ("wg1", "wg3"):
                db.get(models.Connection, ids[key]).online = True
            db.commit()
        sessions(ovpn1=1, ovpn3=2)
        moved, state = poll()
        check(L + "poller: Purchase-1 over cap (1 PPP + its WG) kicks its WG; Purchase-3 at 3/3 is untouched",
              (moved, state), ([(ids["wg1"], False)], {"wg1": (False, True), "wg3": (True, False)}))
        moved, _ = poll()
        check(L + "...no flapping on the next cycle", moved, [])
        sessions(ovpn3=3)
        moved, state = poll()
        check(L + "...Purchase-3 over cap (3 PPP + its WG) kicks only its own WG",
              (moved, state["wg3"]), ([(ids["wg3"], False)], (False, True)))
        sessions(ovpn1=0)
        moved, state = poll()
        check(L + "...Purchase-1 has room again: its WG released; Purchase-3's stays kicked",
              (moved, state), ([(ids["wg1"], True)], {"wg1": (True, False), "wg3": (False, True)}))
        sessions(ovpn3=2)
        moved, state = poll()
        check(L + "...Purchase-3 back to 2 PPP: its WG released", (moved, state["wg3"]),
              ([(ids["wg3"], True)], (True, False)))
        sessions(ovpn1=0, ovpn3=0)
        session_cap_mode.change_mode(env.Session, env.root.id, "legacy", "end of the per-Purchase rule test")

        # ======================================================== the other Purchase paths
        from app.routers import users as users_router
        from app.services import purchase_migration
        with patch.object(user_ops, "provision_connection", side_effect=fake_provision):
            with env.Session() as db:
                user_ops.bulk_create_users(db, "bulkcap", 1, package_id=env.ids.shared)
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="bulkcap1").one()
            bulk = ([p.max_concurrent_sessions for p in db.query(models.Purchase).filter_by(user_id=user.id)],
                    user.max_concurrent_sessions)
        check(L + "bulk create from a package: the Purchase gets the package cap (3); the User too, as legacy "
              "enforces it", bulk, ([3], 3))

        with env.Session() as db:
            user = db.get(models.User, owner_id)
            target = db.query(models.Purchase).filter_by(user_id=owner_id, max_concurrent_sessions=1).one()
            user_ops.replace_purchase_with_package_core(db, user, target, db.get(models.Package, free_id))
            db.commit()
            caps = sorted((p.package_name_snapshot, p.max_concurrent_sessions)
                          for p in db.query(models.Purchase).filter_by(user_id=owner_id))
            replaced = (caps, db.get(models.User, owner_id).max_concurrent_sessions)
        check(L + "apply a package to one service: only that Purchase's cap changes (1 -> 2), the other "
              "Purchases' stay; the User's follows as legacy enforces it", replaced,
              ([("cap-1", 2), ("cap-3", 3), ("cap-None", None)], 2))

        with env.Session() as db:
            user = db.query(models.User).filter_by(username="legacycap").one()
            purchase_migration.migrate_user(db, user)
            db.commit()
            migrated = [p.max_concurrent_sessions for p in db.query(models.Purchase).filter_by(user_id=user.id)]
            pool = wallet_user(db, "convertcap")
            pool.max_concurrent_sessions = 4
            ppp(db, pool, None, "conv-ovpn")
            db.commit()
            converted = users_router.convert_legacy_group(pool.id, schemas.LegacyGroupConvertRequest(), db=db,
                                                          admin=env.root).max_concurrent_sessions
        check(L + "legacy pool -> Purchase (startup migration, manual convert): the cap that governed it, the "
              "User's", (migrated, converted), ([2], 4))


def service(db, user, cap):
    purchase = models.Purchase(user_id=user.id, status=models.UserStatus.active, quota_bytes=0,
                               max_concurrent_sessions=cap, package_name_snapshot=f"cap-{cap}")
    db.add(purchase)
    db.flush()
    return purchase


def ppp(db, user, purchase, name, kind=models.ConnectionType.openvpn, cap=1):
    connection = models.Connection(user_id=user.id, node_id=_NODE[0], type=kind, enabled=True,
                                   purchase_id=purchase.id if purchase else None,
                                   ppp_username=name, ppp_password="pw", max_concurrent_sessions=cap)
    db.add(connection)
    db.flush()
    return connection.id


def wg(db, user, purchase, name):
    connection = models.Connection(user_id=user.id, node_id=_NODE[0], type=models.ConnectionType.wireguard,
                                   enabled=True, purchase_id=purchase.id, wg_peer_name=name,
                                   max_concurrent_sessions=1)
    db.add(connection)
    db.flush()
    return connection.id


_NODE = [None]


def text_update(kind):
    from app import models_provisioning as mp
    return mp.ProvisioningTypeMode.__table__.update().where(
        mp.ProvisioningTypeMode.operation_type == kind).values(mode="legacy")


def wallet_user(db, name):
    from app.services import wallet_accounts
    user = wallet_accounts.create_user_with_wallet(db, username=name, telegram_id=None)
    user.status = models.UserStatus.active
    db.flush()
    return user


with contextlib.ExitStack() as cleanup:
    folder = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="session_cap_"))
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
