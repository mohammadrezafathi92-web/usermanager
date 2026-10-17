"""A package's combined concurrent-session cap reaches the customer it
creates, on every create path, on SQLite AND real MariaDB (CI). Fake
runner; no network.

Reported 2026-10-17: a customer of a 3-user package could not log in with
three devices - RADIUS reads User.max_concurrent_sessions and, when it is
empty, each connection's own cap (1). The panel's create copies the package
cap onto the user; the bot's create and the durable create_user did not.

  legacy bot create_user   user cap = package cap; the absorbed Purchase too
  durable create_user      user cap = the cap FROZEN with the request, even
                           if the package changes before T_final
  approval, no connection  (zero_final) the same, from the approval snapshot
  RADIUS                   cap 3: three sessions accepted, the fourth refused;
                           without a user cap the per-connection 1 still
                           refuses a second login on one credential
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
from app.services import user_ops
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
            caps = (user.max_concurrent_sessions, purchase.max_concurrent_sessions, frozen["changed"])
            db.query(models.Package).filter_by(id=env.ids.shared).update({"max_concurrent_sessions": 3})
            db.commit()
        check(L + "durable create_user: the user gets the cap frozen with the request (3), not the edited 9",
              caps, (3, 3, True))

        # ======================================================== approval with no connection (zero_final)
        item = env.begin("new", "capzero", 700, package_id=free_id, claimed_telegram_id=7402)
        env.run(lambda db: bot_router.create_user(schemas.BotCreateUserRequest(
            username="capzero", telegram_id=7402, package_id=free_id, paid_amount=700, payment_method="card",
            approval_uuid=item["approval_uuid"], execution_token=item["execution_token"], list_price=700),
            db=db, principal=env.internal))
        with env.Session() as db:
            user = db.query(models.User).filter_by(username="capzero").one_or_none()
            state = env.approval(item["approval_uuid"])["state"]
        check(L + "approval without a connection (zero_final): the user gets the package cap (2)",
              (state, user.max_concurrent_sessions if user else None), ("completed", 2))

        # ======================================================== legacy bot create_user (registration off)
        with env.Session() as db:
            db.execute(rv.receipt_approval_runtime_state.update().values(
                registration_mode="off", loyalty_timing_mode="legacy_activation", completeness_generation=0))
            for kind in ("create_user", "purchase", "add_connection"):
                db.execute(text_update(kind))
            db.commit()

        def fake_provision(db, user, node, protocol, flow="", **kwargs):
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
        check(L + "legacy bot create_user: user cap 3, the absorbed Purchase 3",
              (getattr(outcome, "username", outcome), user.max_concurrent_sessions if user else None, purchases),
              ("caplegacy", 3, [3]))

        # ======================================================== RADIUS with the user cap
        radius_server.SessionLocal = env.Session
        server = object.__new__(radius_server.UserManagerRadiusServer)
        server._rejection_log_times = {}
        server._overlimit_attempts = {}             # what __init__ sets (no socket is bound here)
        with env.Session() as db:
            owner = db.query(models.User).filter_by(username="capdurable").one()
            owner.status = models.UserStatus.active
            shared_credential = models.Connection(user_id=owner.id, node_id=env.ids.node,
                                                  type=models.ConnectionType.openvpn, enabled=True,
                                                  ppp_username="cap-ovpn", ppp_password="pw-cap",
                                                  max_concurrent_sessions=1)
            plain = wallet_user(db, "nocap")
            single = models.Connection(user_id=plain.id, node_id=env.ids.node, type=models.ConnectionType.openvpn,
                                       enabled=True, ppp_username="nocap-ovpn", ppp_password="pw-one",
                                       max_concurrent_sessions=1)
            db.add_all([shared_credential, single])
            db.commit()
            shared_id, single_id = shared_credential.id, single.id

        def open_sessions(connection_id, n):
            with env.Session() as db:
                db.query(models.RadiusActiveSession).filter_by(connection_id=connection_id).delete()
                for i in range(n):
                    db.add(models.RadiusActiveSession(connection_id=connection_id, session_id=f"s{connection_id}-{i}",
                                                      client_ip=f"10.9.0.{i + 1}"))
                db.commit()

        accept, reject = packet.AccessAccept, packet.AccessReject
        results = []
        for active in (0, 1, 2, 3):
            open_sessions(shared_id, active)
            results.append(login(server, "cap-ovpn", "pw-cap", f"10.8.0.{active + 1}"))
        check(L + "RADIUS, user cap 3 (one shared credential): logins 1-3 accepted, the 4th refused",
              results, [accept, accept, accept, reject])
        open_sessions(single_id, 1)
        check(L + "RADIUS without a user cap: the per-connection 1 still refuses a second login",
              login(server, "nocap-ovpn", "pw-one", "10.8.1.1"), reject)
        open_sessions(shared_id, 0)
        open_sessions(single_id, 0)


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
