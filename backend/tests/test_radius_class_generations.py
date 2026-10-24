"""Class-cookie session identity (models.RadiusAuthGrant) - real pyrad
packets into the panel's own RADIUS handler, no network.

Every Access-Accept (feature on) carries a fresh nonce in Class, which the
NAS echoes unmodified in all of that session's accounting (RFC 2865 5.25;
RouterOS: "Class - cookie, received in Access-Accept"). Checked here:

  * feature off: no Class, no grant - accounting exactly as before;
  * a retransmitted Access-Request gets the same Class and grant;
  * two genuine sessions under the SAME Acct-Session-Id with IDENTICAL
    counters are each counted exactly once, whatever order their Starts,
    Interims and retransmitted Stops arrive in afterwards;
  * an old session's Stop arriving while a new one under its id is open
    neither counts again nor closes the new one;
  * an unknown Class, another connection's Class, or a Class arriving from
    another (configured, authenticated) NAS changes nothing;
  * Accounting-On from a NAS ends ITS grants only: never-started ones void,
    open ones lost (usage unknown - never taken as zero);
  * a queued renewal activated by a login reconciles its nodes only after
    the auth lock is released (fail-before: with the deferral disabled the
    same check sees the lock held).
"""
import contextlib
import datetime as dt
import logging
import os
import sys
import tempfile
from unittest.mock import patch

_TMP = tempfile.mkdtemp(prefix="um-class-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/app.db")
os.environ.setdefault("SECRET_KEY", "test-only-class-generations-0123456789abcdef")
os.environ.setdefault("RADIUS_ENABLED", "false")
sys.path[:0] = [os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.path.dirname(__file__)]

from pyrad import packet
from pyrad.dictionary import Dictionary
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.config import settings
from app.database import Base, engine as app_engine
from app.services import radius_server, reservation_transition, session_cap_mode, user_ops
from app.services.gate_locks import FileLock, LockTimeout

logging.basicConfig(level=logging.ERROR, format="%(name)s %(levelname)s %(message)s")
failures = []
_dict = Dictionary(radius_server.DICT_PATH)
N1, N2 = "10.20.0.1", "10.20.0.2"


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


Base.metadata.create_all(app_engine)        # the app's own engine (node_gate counters), no noise
engine = create_engine(f"sqlite:///{_TMP}/class.db", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
factory = sessionmaker(bind=engine, autoflush=False)
from app.services import provisioning_schema, receipt_void_schema
assert receipt_void_schema.bootstrap(engine, factory)["ready"]
assert provisioning_schema.bootstrap(engine, factory)["ready"]
radius_server.SessionLocal = factory
server = object.__new__(radius_server.UserManagerRadiusServer)
server._rejection_log_times, server._overlimit_attempts = {}, {}
replies = []
radius_server._send_reply = lambda _srv, pkt, reply: replies.append(reply)
radius_server._notify_ban_async = lambda *a, **k: None

with Session(engine) as db:
    admin = models.AdminUser(username="a", hashed_password="x", role="admin", tree_path="/1/")
    n1 = models.Node(name="n1", type=models.NodeType.mikrotik, mt_host=N1)
    n2 = models.Node(name="n2", type=models.NodeType.mikrotik, mt_host=N2)
    package = models.Package(name="p", duration_days=10, price=1, quota_gb=1)
    db.add_all([admin, n1, n2, package])
    db.flush()
    far = dt.datetime.utcnow() + dt.timedelta(days=10)

    def service(name, **purchase_fields):
        user = models.User(username=name, owner_admin_id=admin.id, status=models.UserStatus.active)
        db.add(user)
        db.flush()
        fields = dict(quota_bytes=10**9, used_bytes=0, expire_at=far, status=models.UserStatus.active,
                      max_concurrent_sessions=5)
        fields.update(purchase_fields)
        p = models.Purchase(user_id=user.id, package_id=package.id, **fields)
        db.add(p)
        db.flush()
        c = models.Connection(user_id=user.id, purchase_id=p.id, node_id=n1.id, enabled=True,
                              type=models.ConnectionType.openvpn, ppp_username=f"{name}-ovpn",
                              ppp_password="pw", max_concurrent_sessions=5)
        db.add(c)
        db.flush()
        return p.id, c.id

    PU, CU = service("u")
    PV, CV = service("v")
    PR, CR = service("r", status=models.UserStatus.expired, expire_at=dt.datetime.utcnow() - dt.timedelta(hours=1),
                     reserved_duration_days=10, reserved_created_at=dt.datetime.utcnow())
    db.commit()


def login(username, sid, source=N1, pkt=None):
    if pkt is None:
        pkt = packet.AuthPacket(id=7, secret=b"s", authenticator=os.urandom(16), dict=_dict)
        pkt["User-Name"] = username
        pkt["User-Password"] = pkt.PwCrypt("pw")
        if sid:
            pkt["Acct-Session-Id"] = sid
        pkt.source = (source, 1812)
    before = len(replies)
    server.HandleAuthPacket(pkt)
    reply = replies[before] if len(replies) > before else None
    cls = reply["Class"][0] if reply is not None and "Class" in reply else None
    return (reply.code if reply is not None else None), cls, pkt    # (a reply with no attributes is falsy)


def acct(username, kind, sid, rx=0, tx=0, cls=None, source=N1):
    pkt = packet.AcctPacket(id=1, secret=b"s", dict=_dict)
    pkt["Acct-Status-Type"] = kind
    if username:
        pkt["User-Name"] = username
    if sid:
        pkt["Acct-Session-Id"] = sid
    if username:
        pkt["Acct-Input-Octets"], pkt["Acct-Output-Octets"] = rx, tx
    if cls is not None:
        pkt["Class"] = cls
    pkt.source = (source, 1813)
    server.HandleAcctPacket(pkt)


def used(pid):
    with Session(engine) as db:
        return int(db.get(models.Purchase, pid).used_bytes or 0)


def rows(cid):
    with Session(engine) as db:
        return db.query(models.RadiusActiveSession).filter_by(connection_id=cid).count()


def grants(**where):
    with Session(engine) as db:
        return sorted(g.state for g in db.query(models.RadiusAuthGrant).filter_by(**where))


lock_dir = tempfile.mkdtemp(prefix="um-class-locks-")
with patch.dict(os.environ, {"UM_LOCK_DIR": lock_dir}):
    # ------------------------------------------------ feature off: nothing new
    with patch.object(settings, "reseller_cancellation_enabled", False):
        code, cls, _ = login("u-ovpn", "81000001")
        check("off: Accept carries no Class and writes no grant", (code, cls, grants()), (packet.AccessAccept, None, []))

    with patch.object(settings, "reseller_cancellation_enabled", True):
        # ------------------------------------------------ retransmitted Access-Request
        code, a, pkt = login("u-ovpn", "81000003")
        _code, again, _ = login("u-ovpn", "81000003", pkt=pkt)       # the NAS did not get the reply
        check("on: Accept carries our Class; its retransmission the SAME one, one grant",
              (code, a is not None and a.startswith(b"um1:"), again == a, grants(connection_id=CU)),
              (packet.AccessAccept, True, True, ["granted"]))

        # ------------------------------------------------ two genuine sessions, same id, identical counters
        acct("u-ovpn", "Start", "81000003", cls=a)
        acct("u-ovpn", "Interim-Update", "81000003", 100, 50, cls=a)
        acct("u-ovpn", "Stop", "81000003", 300, 200, cls=a)
        _c, b, _ = login("u-ovpn", "81000003")                        # router rebooted: the id again
        acct("u-ovpn", "Start", "81000003", cls=b)
        acct("u-ovpn", "Stop", "81000003", 300, 200, cls=b)           # the very same counters
        check("two sessions, same id and final counters: each counted once (500 + 500)",
              (used(PU), rows(CU), grants(connection_id=CU)), (1000, 0, ["stopped", "stopped"]))
        for kind, rx, tx, cls in (("Stop", 300, 200, a), ("Stop", 300, 200, b), ("Interim-Update", 100, 50, a),
                                  ("Start", 0, 0, a), ("Interim-Update", 100, 50, b), ("Stop", 300, 200, a),
                                  ("Start", 0, 0, b), ("Stop", 300, 200, b)):
            acct("u-ovpn", kind, "81000003", rx, tx, cls=cls)
        check("...any replay order afterwards (Stops, late Interims, late Starts): nothing more, nothing open",
              (used(PU), rows(CU)), (1000, 0))

        # ------------------------------------------------ an old Stop while a new session under its id is open
        _c, c1, _ = login("u-ovpn", "81000009")
        acct("u-ovpn", "Start", "81000009", cls=c1)
        acct("u-ovpn", "Stop", "81000009", 10, 10, cls=c1)
        _c, c2, _ = login("u-ovpn", "81000009")
        acct("u-ovpn", "Start", "81000009", cls=c2)
        acct("u-ovpn", "Interim-Update", "81000009", 5, 5, cls=c2)
        acct("u-ovpn", "Stop", "81000009", 10, 10, cls=c1)           # the old one's Stop again
        acct("u-ovpn", "Start", "81000009", cls=c1)                   # and its late Start
        check("old Stop/Start under a reused id: no double count, the new session stays open",
              (used(PU), rows(CU), grants(connection_id=CU).count("open")), (1000 + 20 + 10, 1, 1))

        # ------------------------------------------------ foreign cookies change nothing
        before = (used(PU), rows(CU), grants(connection_id=CU))
        acct("u-ovpn", "Interim-Update", "81000009", 999, 999, cls=b"um1:" + b"0" * 32)   # unknown nonce
        _c, other, _ = login("v-ovpn", "81000011")
        acct("u-ovpn", "Stop", "81000009", 999, 999, cls=other)                           # another connection's
        acct("u-ovpn", "Stop", "81000009", 999, 999, cls=c2, source=N2)                   # right cookie, wrong NAS
        check("unknown Class / another connection's / from another NAS: nothing changes",
              ((used(PU), rows(CU), grants(connection_id=CU)), grants(connection_id=CV)),
              (before, ["granted"]))

        # ------------------------------------------------ the connection's purchase history moves mid-session
        with Session(engine) as db:
            db.get(models.Purchase, PU).history_key = "rekeyed-" + "0" * 28    # e.g. a history-key backfill
            db.commit()
        acct("u-ovpn", "Interim-Update", "81000009", 8, 8, cls=c2)
        check("a purchase re-key mid-session does not cut its accounting off (+6)", used(PU), 1036)

        # ------------------------------------------------ the NAS restarts (evidence only)
        def restarted(cid):
            with Session(engine) as db:
                return sorted((g.state, g.nas_restart_at is not None)
                              for g in db.query(models.RadiusAuthGrant).filter_by(connection_id=cid))
        acct(None, "Accounting-On", None, source=N2)
        check("Accounting-On from ANOTHER NAS: this node's grants untouched",
              (restarted(CU).count(("open", True)), restarted(CV)), (0, [("granted", False)]))
        acct(None, "Accounting-On", None, source=N1)
        check("Accounting-On from its NAS: its unended grants marked, none ended by it",
              (restarted(CU).count(("open", True)), restarted(CV)), (1, [("granted", True)]))
        acct("v-ovpn", "Interim-Update", "81000011", 1000, 2000, cls=other)
        check("...a session Accepted around the reboot still counts (auth/acct not ordered: 3000)",
              (used(PV), restarted(CV)), (3000, [("open", False)]))
        acct("u-ovpn", "Stop", "81000009", 50, 50, cls=c2)
        check("...and so does the open one's Stop (+84), whatever the restart said",
              (used(PU), grants(connection_id=CU).count("open")), (1120, 0))

        # ------------------------------------------------ accounting under another id than the Access-Request's
        _c, d1, _ = login("u-ovpn", "AAAA0001")
        acct("u-ovpn", "Start", "BBBB0001", cls=d1)
        acct("u-ovpn", "Stop", "BBBB0001", 7, 3, cls=d1)
        check("the Accept's cookie, not its id, names the session: counted under the accounting id (+10)",
              used(PU), 1130)

        # ------------------------------------------------ a NAS that is no single configured node
        code, cls, _ = login("v-ovpn", "81000031", source="10.99.0.1")
        with Session(engine) as db:
            unbound = db.query(models.RadiusAuthGrant).filter_by(connection_id=CV, acct_session_id="81000031").all()
        check("an Accept from no configured node: no cookie, but its grant is recorded (node unknown -> "
              "a cancellation fails closed instead of never seeing it)",
              (code, cls, [(g.state, g.node_id) for g in unbound]), (packet.AccessAccept, None, [("granted", None)]))

        # ------------------------------------------------ queued renewal: node work after the auth lock
        held = []

        def watch(_db, purchase):
            probe = FileLock(session_cap_mode.lock_path())
            try:
                probe.acquire(shared=False, timeout=0)
                probe.release()
                held.append(False)
            except LockTimeout:
                held.append(True)

        with patch.object(user_ops, "reconcile_purchase_connections", watch):
            code, _cls, _ = login("r-ovpn", "81000021")
        check("a login activating a queued renewal is accepted; its node reconcile ran AFTER the auth lock",
              (code, held), (packet.AccessAccept, [False]))
        # fail-before: the same check, with the deferral switched off (the old behaviour)
        with Session(engine) as db:
            p = db.get(models.Purchase, PR)
            p.status, p.expire_at = models.UserStatus.expired, dt.datetime.utcnow() - dt.timedelta(hours=1)
            p.reserved_duration_days, p.reserved_created_at = 10, dt.datetime.utcnow()
            db.commit()
        held.clear()
        with patch.object(user_ops, "reconcile_purchase_connections", watch), \
                patch.object(reservation_transition, "deferred_reconciliation", contextlib.nullcontext):
            login("r-ovpn", "81000022")
        check("(fail-before) without the deferral the node reconcile runs under the auth lock - detected",
              held, [True])

engine.dispose()
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
