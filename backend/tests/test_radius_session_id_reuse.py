"""RADIUS accounting when an Acct-Session-Id comes back, with the
unpaid-cancellation feature OFF (it is every customer's accounting).

A closed session is remembered (radius_closed_sessions) so a retransmitted
Stop or a late Interim is not counted twice. The same id can still name a
NEW session later - RouterOS restarts its session counter on reboot - and a
Start can arrive after its own Stop. Neither may break accounting:

  * a reused id: its Start, Interim and Stop are counted and its Stop is
    committed (the second Stop of that id once failed on the unique key, so
    the whole Stop was rolled back - its usage lost, a phantom session left);
  * a reused id whose Start was lost: its usage is counted, not dropped as
    "already closed" for days;
  * a late Start after its own Stop: no phantom open session (it held the
    customer's concurrent-session cap) and a retransmitted Stop after it
    still counts nothing again.

Real pyrad packets into the panel's own handler. SQLite and, when
MARIADB_TEST_URL is set (always in CI), an isolated real MariaDB database.
"""
import datetime as dt
import os
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(__file__)]
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "test-only-session-id-reuse-0123456789abcdef")
import _no_network
_no_network.install([os.environ.get("MARIADB_TEST_URL", "").strip()])
import _mariadb_scratch as scratch

from pyrad import packet
from pyrad.dictionary import Dictionary
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.config import settings
from app.database import Base
from app.services import radius_server

failures = []
_dict = Dictionary(radius_server.DICT_PATH)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def run(engine, directory):
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    radius_server.SessionLocal = factory
    server = object.__new__(radius_server.UserManagerRadiusServer)
    server._rejection_log_times, server._overlimit_attempts = {}, {}
    name = engine.dialect.name

    with Session(engine) as db:
        admin = models.AdminUser(username="a", hashed_password="x", role="admin", tree_path="/1/")
        node = models.Node(name="mt", type=models.NodeType.mikrotik)
        package = models.Package(name="p", duration_days=10, price=1, quota_gb=1)
        db.add_all([admin, node, package])
        db.flush()
        user = models.User(username="u", owner_admin_id=admin.id, status=models.UserStatus.active)
        db.add(user)
        db.flush()
        far = dt.datetime.utcnow() + dt.timedelta(days=10)
        p = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=10**9, used_bytes=0, expire_at=far,
                            status=models.UserStatus.active, max_concurrent_sessions=1)
        db.add(p)
        db.flush()
        c = models.Connection(user_id=user.id, purchase_id=p.id, node_id=node.id, enabled=True,
                              type=models.ConnectionType.openvpn, ppp_username="u-ovpn", ppp_password="pw",
                              max_concurrent_sessions=1)
        db.add(c)
        db.commit()
        cid, pid = c.id, p.id

    answered = []

    def acct(kind, sid, rx=0, tx=0, seconds=None):
        pkt = packet.AcctPacket(id=1, secret=b"s", dict=_dict)
        pkt["User-Name"] = "u-ovpn"
        pkt["Acct-Status-Type"] = kind
        pkt["Acct-Session-Id"] = sid
        pkt["Acct-Input-Octets"] = rx
        pkt["Acct-Output-Octets"] = tx
        if seconds is not None:
            pkt["Acct-Session-Time"] = seconds
        pkt.source = ("10.20.0.1", 1813)
        with patch.object(radius_server, "_send_reply", lambda *a: answered.append(kind)):
            server.HandleAcctPacket(pkt)

    def login(session=None):
        pkt = packet.AuthPacket(id=1, secret=b"s", authenticator=os.urandom(16), dict=_dict)
        pkt["User-Name"] = "u-ovpn"
        if session:
            pkt["Acct-Session-Id"] = session
        pkt["User-Password"] = pkt.PwCrypt("pw")
        pkt.source = ("10.30.0.9", 1812)
        replies = []
        with patch.object(radius_server, "_send_reply", side_effect=lambda s, p, r: replies.append(r)), \
                patch.object(radius_server, "_notify_ban_async", lambda *a, **k: None):
            server.HandleAuthPacket(pkt)
        return replies[-1].code if replies else None

    def used():
        with Session(engine) as db:
            return int(db.get(models.Purchase, pid).used_bytes or 0)

    def open_rows():
        with Session(engine) as db:
            return db.query(models.RadiusActiveSession).filter_by(connection_id=cid).count()

    def age_closed(sid, minutes):
        with Session(engine) as db:
            db.query(models.RadiusClosedSession).filter_by(connection_id=cid, session_id=sid).update(
                {"closed_at": dt.datetime.utcnow() - dt.timedelta(minutes=minutes)})
            db.commit()

    with patch.object(settings, "reseller_cancellation_enabled", False), \
            patch.dict(os.environ, {"UM_LOCK_DIR": directory}):
        # ---------------------------------------------- a reused id, with its Start
        acct("Start", "r1")
        acct("Interim-Update", "r1", 50, 30, 60)
        acct("Stop", "r1", 60, 40, 90)
        check(f"[{name}] first session r1 counted", (used(), open_rows()), (100, 0))
        age_closed("r1", 30)                     # the router rebooted since; its counter came round again
        answered.clear()
        acct("Start", "r1")
        acct("Interim-Update", "r1", 10, 10, 60)
        acct("Stop", "r1", 30, 20, 120)
        check(f"[{name}] reused id r1: Start, Interim and Stop counted, the Stop committed and answered",
              (used(), open_rows(), answered), (150, 0, ["Start", "Interim-Update", "Stop"]))
        acct("Stop", "r1", 30, 20, 120)          # retransmitted: our reply was lost
        check(f"[{name}] ...and its retransmitted Stop counts nothing again", used(), 150)

        # ---------------------------------------------- a reused id whose Start was lost
        acct("Start", "r2")
        acct("Stop", "r2", 100, 100, 600)
        age_closed("r2", 30)
        acct("Interim-Update", "r2", 20, 20, 60)  # a new session of 60 s, its Start never arrived
        check(f"[{name}] reused id r2 without its Start: an Interim below the old finals is ambiguous, not counted",
              used(), 150 + 200)
        acct("Stop", "r2", 30, 30, 70)
        check(f"[{name}] ...through its Stop, and nothing stays open", (used(), open_rows()), (150 + 200 + 60, 0))

        # ---------------------------------------------- a late Start, more than a minute after its Stop
        base = used()
        acct("Start", "r3")
        acct("Stop", "r3", 40, 40, 5)
        age_closed("r3", 2)
        acct("Start", "r3")                       # the first Start's retransmission, 2 min late
        check(f"[{name}] a late Start is not guessed away by time: a row opens (as on main)", open_rows(), 1)
        acct("Stop", "r3", 40, 40, 5)             # and that session's Stop retransmitted after it
        check(f"[{name}] ...its retransmitted Stop counts nothing and closes that phantom",
              (used(), open_rows()), (base + 80, 0))
        check(f"[{name}] ...so the customer may log in (cap 1, no session really open)",
              login(), packet.AccessAccept)

        # ---------------------------------------------- a new session under the id right after a reboot
        base = used()
        acct("Start", "r4")
        acct("Stop", "r4", 40, 40, 5)
        acct("Start", "r4")                       # rebooted at once: the same id again, seconds later
        acct("Interim-Update", "r4", 10, 10, 3)   # below the old finals, began before that Stop by the clock
        check(f"[{name}] same id right after a reboot: an ambiguous Interim is not counted yet",
              (used(), open_rows()), (base + 80, 1))
        acct("Interim-Update", "r4", 50, 45, 63)  # above the old finals: provably the new session
        acct("Stop", "r4", 60, 50, 70)
        check(f"[{name}] ...the new session's whole usage (110) is counted from zero, nothing lost",
              (used(), open_rows()), (base + 80 + 110, 0))

        # ---------------------------------------------- no Acct-Session-Time at all
        base = used()
        acct("Start", "r5")
        acct("Stop", "r5", 100, 100)
        acct("Interim-Update", "r5", 90, 90)      # late Interim of that session? or a new one? unknown
        check(f"[{name}] no session time: an Interim at or below the finals is not counted",
              (used(), open_rows()), (base + 200, 0))
        acct("Start", "r5")
        acct("Interim-Update", "r5", 10, 10)
        acct("Stop", "r5", 30, 20)                # a Stop other than the closed one's final report
        check(f"[{name}] ...a new session under it is still counted whole by its Stop",
              (used(), open_rows()), (base + 200 + 50, 0))
        acct("Stop", "r5", 30, 20)                # that Stop retransmitted
        check(f"[{name}] ...and that Stop retransmitted is not counted twice", used(), base + 250)

        # ---------------------------------------------- a very late Interim (arrival time is no proof)
        base = used()
        acct("Start", "r6")
        acct("Stop", "r6", 10, 10, 300)
        age_closed("r6", 2)                       # its Interim of second 60 arrives 2 minutes after the Stop
        acct("Interim-Update", "r6", 5, 5, 60)
        check(f"[{name}] a late Interim is not counted again, however late, and opens no session",
              (used(), open_rows()), (base + 20, 0))

        # ---------------------------------------------- flag off: nothing new per login
        with Session(engine) as db:
            before = db.query(models.RadiusAuthGrant).count()
        check(f"[{name}] flag off: a login is accepted as before", login("r7"), packet.AccessAccept)
        with Session(engine) as db:
            check(f"[{name}] ...and writes no grant (Class identity: test_radius_class_generations)",
                  db.query(models.RadiusAuthGrant).count(), before)
    engine.dispose()


with TemporaryDirectory() as directory:
    run(create_engine(f"sqlite:///{directory}/reuse.db"), directory)
url = os.environ.get("MARIADB_TEST_URL")
if url:
    engine = scratch.claim(url)
    try:
        with TemporaryDirectory() as directory:
            run(engine, directory)
    finally:
        scratch.release(engine)
elif os.environ.get("CI"):
    raise SystemExit("CI requires real MariaDB")
else:
    print("SKIP  real MariaDB execution (MARIADB_TEST_URL not set, not in CI)")
assert not _no_network.attempts
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
