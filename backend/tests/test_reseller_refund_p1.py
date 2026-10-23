"""Unpaid-service cancellation: the four review findings of 2026-10-23
(Codex), as deterministic regressions. Fake RouterOS and fake NAS, REAL
RADIUS packets into the panel's own handler; no network.

  1. An Access-Accept decided before the cancellation blocked the login but
     sent after it: the cancellation must not finish around it - the
     session that Accept admits is ended and its usage counted.
  2. A session the router reports whose Start (and Interims) never reached
     the panel: there is no RADIUS row for it, which is not "no usage".
     Without its Stop the operation waits, unpaid; with it, it is counted.
  3. (test_radius_session_id_reuse.py - accounting with the feature off.)
  4. PPTP: the panel's RADIUS authenticates and accounts no PPTP session at
     all, so a service with a PPTP connection is refused before anything
     is touched - a refused login is not proof that PPTP was handled.
"""
import datetime as dt
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tempfile
_TMP = tempfile.mkdtemp(prefix="um-p1-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/app.db")    # the app's own engine (node_gate counters)
os.environ.setdefault("SECRET_KEY", "test-only-reseller-refund-p1-0123456789abcdef")
os.environ.setdefault("RADIUS_ENABLED", "false")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from pyrad import packet
from pyrad.dictionary import Dictionary
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import models
from app.config import settings
from app.database import Base
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from app.models_reseller_refund import ResellerSaleBasis
from app.services import radius_server, reseller_refund_settlement as settlement
from app.services import reseller_refund_worker as worker
from app.services.reseller_refund import bind_sale

failures = []
_dict = Dictionary(radius_server.DICT_PATH)


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class Router:
    def __init__(self, nas):
        self.nas, self.peers, self.active = nas, {}, {}

    def get_system_resources(self):
        return {"uptime": "52w"}

    def list_peers(self, interface):
        return [dict(p, **{".id": pid}) for pid, p in self.peers.items()]

    def set_peer_disabled(self, handle, disabled):
        self.peers[handle]["disabled"] = bool(disabled)

    def remove_peer(self, handle):
        self.peers.pop(handle)

    def remove_simple_queue(self, name):
        pass

    def ppp_session_ids(self, username):
        return [f"0x{sid}".upper() if sid else None for sid, _rx, _tx in self.active.get(username, [])]

    def kick_ppp_session(self, username):
        sessions = self.active.pop(username, [])
        for sid, rx, tx in sessions:
            if username not in self.nas.drop_stop:
                self.nas.acct(username, "Stop", sid, rx, tx)
        return bool(sessions)


class Nas:
    """Replies are collected by one module-wide _send_reply (set once, never
    patched per call: the login and the cancellation run in two threads)."""

    def __init__(self):
        self.server = object.__new__(radius_server.UserManagerRadiusServer)
        self.server._rejection_log_times, self.server._overlimit_attempts = {}, {}
        self.drop_stop, self.replies, self.on_accept, self.classes = set(), [], None, {}

    def send_reply(self, _srv, pkt, reply):
        self.replies.append((pkt.get("User-Name", [None])[0], reply.code))
        if reply.code == packet.AccessAccept and "Class" in reply and "Acct-Session-Id" in pkt:
            # what RouterOS does: the Accept's Class goes into every
            # Accounting-Request of that session, unmodified
            self.classes[(_to_str(pkt["User-Name"][0]), _to_str(pkt["Acct-Session-Id"][0]))] = reply["Class"][0]
        if reply.code == packet.AccessAccept and self.on_accept:
            self.on_accept(_to_str(pkt["User-Name"][0]))

    def acct(self, username, kind, session, rx=0, tx=0, cls=None):
        pkt = packet.AcctPacket(id=1, secret=b"s", dict=_dict)
        pkt["User-Name"], pkt["Acct-Status-Type"], pkt["Acct-Session-Id"] = username, kind, session
        pkt["Acct-Input-Octets"], pkt["Acct-Output-Octets"] = rx, tx
        cls = cls if cls is not None else self.classes.get((username, session))
        if cls is not None:
            pkt["Class"] = cls
        pkt.source = (NAS_IP, 1813)
        self.server.HandleAcctPacket(pkt)

    def login(self, username, session=None):
        pkt = packet.AuthPacket(id=1, secret=b"s", authenticator=os.urandom(16), dict=_dict)
        pkt["User-Name"] = username
        if session:
            pkt["Acct-Session-Id"] = session
        pkt["User-Password"] = pkt.PwCrypt("pw")
        pkt.source = (NAS_IP, 1812)          # the NAS (router) is the RADIUS client
        before = len(self.replies)
        self.server.HandleAuthPacket(pkt)
        # the auth reply itself (an Accept's session Start is answered after it)
        return self.replies[before][1] if len(self.replies) > before else None


NAS_IP = "10.20.0.1"


def _to_str(value):
    return value.decode() if isinstance(value, bytes) else value


def scenario(directory):
    engine = create_engine(f"sqlite:///{directory}/p1.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    from app.database import engine as app_engine
    from app import models_provisioning
    Base.metadata.create_all(app_engine)      # node_gate's counters live on the app's own engine
    models_provisioning.ProvisioningGateStat.__table__.create(app_engine, checkfirst=True)
    factory = sessionmaker(bind=engine, autoflush=False)
    from app.services import provisioning_schema, receipt_void_schema
    assert receipt_void_schema.bootstrap(engine, factory)["ready"]
    assert provisioning_schema.bootstrap(engine, factory)["ready"]     # gate 'off': writes run inline
    radius_server.SessionLocal = factory
    nas = Nas()
    router = Router(nas)
    elsewhere = Router(nas)        # a second router pointing at this panel

    @contextmanager
    def client(node):
        yield elsewhere if node.name == "mt2" else router

    with Session(engine) as db:
        actor = models.AdminUser(username="seller", hashed_password="x", role="seller", tree_path="/1/", balance=0)
        node = models.Node(name="mt", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0", mt_host=NAS_IP)
        package = models.Package(name="p", duration_days=10, price=100000, quota_gb=1)
        node2 = models.Node(name="mt2", type=models.NodeType.mikrotik, mt_host="10.20.0.2", mt_radius_secret="s2")
        db.add_all([actor, node, package, node2])
        db.flush()
        far = dt.datetime.utcnow() + dt.timedelta(days=10)
        hour_ago = dt.datetime.utcnow() - dt.timedelta(hours=1)

        def service(name, kinds):
            user = models.User(username=name, owner_admin_id=actor.id, balance=0, purchase_count=1,
                               status=models.UserStatus.active)
            db.add(user)
            db.flush()
            p = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=1000, used_bytes=0,
                                expire_at=far, status=models.UserStatus.active, max_concurrent_sessions=3)
            db.add(p)
            db.flush()
            for kind in kinds:
                tag = f"{name}-{kind}"
                c = models.Connection(user_id=user.id, purchase_id=p.id, node_id=node.id, enabled=True,
                                      type=getattr(models.ConnectionType, kind), max_concurrent_sessions=1,
                                      created_at=hour_ago)
                if kind == "wireguard":
                    c.wg_peer_name, c.wg_public_key, c.wg_client_address = tag, f"key-{tag}", f"10.0.{len(router.peers)}.2/32"
                    router.peers[f"*{tag}"] = {"interface": "wg0", "comment": tag, "public-key": f"key-{tag}",
                                              "allowed-address": c.wg_client_address, "disabled": False,
                                              "tx": "0", "rx": "0"}
                else:
                    c.ppp_username, c.ppp_password = tag, "pw"
                db.add(c)
            db.flush()
            debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
            sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id,
                                     purchase_id=p.id)
            db.add_all([debit, sale])
            db.flush()
            bind_sale(db, p, debit, package, sale_entry=sale, purchase_count_delta=1)
            db.query(ResellerSaleBasis).filter_by(purchase_id=p.id).update({"created_at": hour_ago})
            return p.id

        inflight = service("x", ["openvpn"])
        lost = service("y", ["openvpn"])
        unbound = service("w", ["openvpn"])
        reused = service("v", ["openvpn"])
        legacy = service("t", ["openvpn"])
        roaming = service("s", ["openvpn"])
        pptp = service("z", ["wireguard", "pptp"])
        db.commit()
        aid = actor.id

    def run(pid):
        with Session(engine) as db:
            try:
                return worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
            except HTTPException as exc:
                return exc.status_code, exc.detail

    def final_bytes(pid):
        with Session(engine) as db:
            op = db.query(Operation).filter(
                (Operation.purchase_id == pid) | (Operation.retired_purchase_id == pid)).one()
            return [s.final_total_bytes for s in db.query(Step).filter_by(operation_id=op.id)]

    def refunds():
        with Session(engine) as db:
            return db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count()

    def reason(outcome):
        if isinstance(outcome, tuple):
            return outcome[1]["reason"] if isinstance(outcome[1], dict) else outcome[1]
        return outcome

    original_reply = radius_server._send_reply
    radius_server._send_reply = nas.send_reply
    try:
        with patch.object(settings, "reseller_cancellation_enabled", True), \
                patch.object(worker.sys, "platform", "linux"), \
                patch.dict(os.environ, {"UM_LOCK_DIR": directory}), \
                patch.object(worker.MikrotikClient, "for_node", client), \
                patch.object(radius_server, "_notify_ban_async", lambda *a, **k: None), \
                patch.object(worker, "PPP_STOP_WAIT_SECONDS", 1):
            # ---------------------------------------------- 1. an Accept in flight
            # The NAS brings the admitted session up LATE - later than any
            # waiting window: nothing shows on the router or in accounting
            # while the cancellation runs.
            deciding, release = threading.Event(), threading.Event()
            real_check = radius_server.UserManagerRadiusServer._check_password

            def slow_check(self, pkt, password):
                deciding.set()
                release.wait(10)
                return real_check(self, pkt, password)

            result, reads, first_read = {}, [], threading.Event()
            real_sessions = worker._ppp_sessions

            def watched_sessions(connection):
                # Once the login is blocked: was the in-flight Accept already
                # sent when the router is read? (A read-only check before the
                # block may run while the login is undecided.)
                with Session(engine) as db:
                    blocked = db.query(Step.id).filter(Step.connection_id == connection.id,
                                                       Step.blocked_at.isnot(None)).first() is not None
                if blocked:
                    reads.append(any(code == packet.AccessAccept for _, code in nas.replies))
                    first_read.set()
                return real_sessions(connection)

            with patch.object(radius_server.UserManagerRadiusServer, "_check_password", slow_check), \
                    patch.object(worker, "_ppp_sessions", watched_sessions):
                login = threading.Thread(target=lambda: result.setdefault("login", nas.login("x-openvpn", "g1")))
                login.start()
                deciding.wait(5)
                cancel = threading.Thread(target=lambda: result.setdefault("cancel", run(inflight)))
                cancel.start()
                # Old code reads the router while the login is still undecided;
                # fixed code cannot get past the block until it is answered.
                first_read.wait(5)
                release.set()
                login.join(15)
                cancel.join(30)
            check("1. the router is read only after the login being decided was answered",
                  reads[:1], [True])
            check("1. ...that login, decided before the block, was accepted", result.get("login"), packet.AccessAccept)
            check("1. ...its session not up yet: NOT completed - pending on the unresolved Accept, unpaid",
                  (reason(result.get("cancel")), refunds()), ("ppp_login_unresolved", 0))
            time.sleep(1.2)
            check("1. ...and no elapsed time completes it", (reason(run(inflight)), refunds()),
                  ("ppp_login_unresolved", 0))
            router.active["x-openvpn"] = [("g1", 70, 30)]       # the session finally comes up
            nas.acct("x-openvpn", "Start", "g1")
            done = run(inflight)
            check("1. the session reports: the resume ends it and completes", reason(done) if not isinstance(
                done, dict) else done.get("state"), "completed")
            check("1. ...none survives the deleted service; its usage (70 + 30) is final",
                  (router.active.get("x-openvpn"), final_bytes(inflight), refunds()), (None, [100], 1))

            # ---------------------------------------------- 1c. a NEW Accept under a reused session id
            nas.login("v-openvpn", "v1")
            old = nas.classes[("v-openvpn", "v1")]
            nas.acct("v-openvpn", "Start", "v1")
            nas.acct("v-openvpn", "Stop", "v1", 40, 40)          # the old session under v1, closed
            check("1c. the router rebooted: a new login reuses id v1", nas.login("v-openvpn", "v1"),
                  packet.AccessAccept)
            nas.acct("v-openvpn", "Stop", "v1", 40, 40, cls=old)  # the OLD session's Stop retransmitted
            nas.acct("v-openvpn", "Start", "v1", cls=old)         # ...and its late Start, then its Stop again
            nas.acct("v-openvpn", "Stop", "v1", 40, 40, cls=old)
            def blocked_steps(name):
                with Session(engine) as db:
                    cid = db.query(models.Connection.id).filter_by(ppp_username=name).scalar()
                    return db.query(Step.id).filter(Step.connection_id == cid, Step.blocked_at.isnot(None)).count()
            check("1c. ...old-session packets under v1 do not resolve the new Accept: refused before "
                  "anything is touched (never blocked into an operation that could not end), unpaid",
                  (reason(run(reused)), blocked_steps("v-openvpn"), refunds()), ("ppp_session_unverified", 0, 1))

            # ---------------------------------------------- 1b. an Accept without Acct-Session-Id
            check("1b. a login whose request carries no Acct-Session-Id", nas.login("w-openvpn"),
                  packet.AccessAccept)
            check("1b. ...its session never reported: refused before anything is touched, unpaid",
                  (reason(run(unbound)), blocked_steps("w-openvpn"), refunds()), ("ppp_session_unverified", 0, 1))

            # ---------------------------------------------- 2. Start and Interims lost
            nas.login("y-openvpn", "s7")                          # an earlier session, ended
            nas.acct("y-openvpn", "Start", "s7")
            nas.acct("y-openvpn", "Stop", "s7", 5, 5)
            check("2. a login whose Start and Interims never reach the panel", nas.login("y-openvpn", "s9"),
                  packet.AccessAccept)
            router.active["y-openvpn"] = [("s9", 300, 200)]      # up on the router; no accounting of it at all
            nas.drop_stop = {"y-openvpn"}
            paid = refunds()
            first = run(lost)
            check("2. ...ended, its Stop lost too: pending (no usage is NOT zero usage)",
                  reason(first), "ppp_login_unresolved")
            check("2. ...nothing paid", refunds(), paid)
            again = run(lost)
            check("2. ...a resume without the Stop still waits, unpaid", (reason(again), refunds()),
                  ("ppp_login_unresolved", paid))
            nas.acct("y-openvpn", "Stop", "s7", 5, 5)            # the EARLIER session's Stop again, late
            check("2. ...another (earlier) session's late Stop does not stand in for it",
                  (reason(run(lost)), refunds()), ("ppp_login_unresolved", paid))
            nas.acct("y-openvpn", "Stop", "s9", 300, 200)        # ITS Stop finally arrives
            done = run(lost)
            check("2. its own Stop arrives: the resume completes", isinstance(done, dict) and done.get("state"),
                  "completed")
            check("2. ...with that session's whole usage (500, + the earlier session's 10) counted, paid once",
                  (final_bytes(lost), refunds()), ([510], paid + 1))

            # ---------------------------------------------- 2b. a session without our cookie
            nas.acct("t-openvpn", "Start", "t1")                 # e.g. from before the feature was on
            router.active["t-openvpn"] = [("t1", 1, 1)]
            refused = run(legacy)
            with Session(engine) as db:
                blocked = db.query(Step.id).filter(Step.blocked_at.isnot(None),
                                                   Step.connection_id.in_(
                                                       db.query(models.Connection.id).filter_by(
                                                           ppp_username="t-openvpn"))).count()
            check("2b. a session without a Class grant: refused before anything is touched (not blocked/kicked)",
                  (reason(refused), blocked, router.active.get("t-openvpn"), refunds()),
                  ("ppp_session_unverified", 0, [("t1", 1, 1)], paid + 1))

            # ---------------------------------------------- 2c. the credential up on ANOTHER router
            elsewhere.active["s-openvpn"] = [("q1", 0, 0)]       # no row (pruned), no grant: invisible to accounting
            refused = run(roaming)
            check("2c. a session of it on another router: refused before anything is touched, unpaid",
                  (reason(refused), blocked_steps("s-openvpn"), refunds()), ("ppp_session_foreign_node", 0, paid + 1))

            # ---------------------------------------------- 4. PPTP
            with Session(engine) as db:
                check("4. a service with a PPTP connection is refused (RADIUS has no PPTP)",
                      worker.availability(db, db.get(models.Purchase, pptp)),
                      "reseller_cancellation_protocol_unsupported")
            refused = run(pptp)
            with Session(engine) as db:
                ops = db.query(Operation).filter_by(purchase_id=pptp).count()
            check("4. ...before anything is touched: no operation, its peer still enabled",
                  (refused, ops, router.peers["*z-wireguard"]["disabled"]),
                  ((409, "reseller_cancellation_protocol_unsupported"), 0, False))
            check("4. (documented gap) a PPTP login is not authenticated by this panel's RADIUS at all",
                  nas.login("z-pptp"), packet.AccessReject)
    finally:
        radius_server._send_reply = original_reply
    engine.dispose()


with TemporaryDirectory() as directory:
    scenario(directory)
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
