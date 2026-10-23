"""Unpaid-service cancellation of a MIXED service: WireGuard + OpenVPN +
L2TP. (Production's sales also carry PPTP, which this panel's RADIUS does
not authenticate or account at all: such a service is refused before
anything is touched - test_reseller_refund_p1. Production mixed
eligibility is therefore still incomplete.)

A fake RouterOS holds the WireGuard peers and /ppp/active; a fake NAS sends
REAL RADIUS accounting packets (pyrad) to the panel's own RADIUS handler,
so usage arrives exactly as in production. No network.

  * only the target Purchase's connections are touched; the customer's
    other service, its peer, its PPP session and the wallet are not;
  * from the start a PPP login of the service is refused - also with the
    feature switched off while pending;
  * an Accounting Stop that never comes: pending with the reason
    ppp_accounting_stop_missing and since when; no refund; the 15-minute
    prune and the poller's /ppp/active reconciliation do NOT remove that
    session (neither proves the final usage); a resume keeps waiting;
  * the Stop arrives late: the resume finishes - credited exactly once,
    PPP usage included; a retry returns the stored result;
  * a retransmitted Stop or an Interim after the Stop never counts again;
  * a router reboot: WireGuard continuity is refused before anything is
    touched; PPP sessions that vanished without a Stop leave it pending;
  * a partial failure (the WireGuard delete) keeps every component's state;
    the resume continues the same operation and pays once.
"""
import datetime as dt
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import tempfile
_TMP = tempfile.mkdtemp(prefix="um-mixed-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/app.db")
os.environ.setdefault("SECRET_KEY", "test-only-reseller-refund-mixed-0123456789abcdef")
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
from app.models_reseller_refund import ResellerRefundOperation as Operation
from app.services import quota_manager, radius_server, reseller_refund_status as status
from app.services import reseller_refund_worker as worker
from app.services.reseller_refund import bind_sale

failures = []


def check(label, got, expected=True):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


_dict = Dictionary(radius_server.DICT_PATH)


class Router:
    """One MikroTik: WireGuard peers and the PPP sessions it carries."""

    def __init__(self, nas):
        self.nas = nas
        self.peers = {}
        self.active = {}            # ppp username -> [(session id, in, out)]
        self.uptime = "52w"
        self.fail_remove = 0
        self.removed = []

    def get_system_resources(self):
        return {"uptime": self.uptime}

    def list_peers(self, interface):
        return [dict(p, **{".id": pid}) for pid, p in self.peers.items()]

    def set_peer_disabled(self, handle, disabled):
        self.peers[handle]["disabled"] = bool(disabled)

    def remove_peer(self, handle):
        if self.fail_remove:
            self.fail_remove -= 1
            raise RuntimeError("injected remove failure")
        self.removed.append(self.peers.pop(handle)["comment"])

    def remove_simple_queue(self, name):
        pass

    def ppp_session_ids(self, username):
        return [sid for sid, _rx, _tx in self.active.get(username, [])]

    def kick_ppp_session(self, username):
        sessions = self.active.pop(username, [])
        for sid, rx, tx in sessions:
            if username not in self.nas.drop_stop:
                self.nas.acct(username, "Stop", sid, rx, tx)
        return bool(sessions)

    def list_active_ppp_usernames(self):
        return set(self.active)

    def reboot(self):
        self.active.clear()            # sessions gone - and no Stop was sent
        self.uptime = "30s"


NAS_IP = "10.20.0.1"


class Nas:
    """A RouterOS-like NAS: a session starts with its Access-Request (which
    names the coming Acct-Session-Id) and every Accounting-Request of it
    echoes the Accept's Class unmodified."""

    def __init__(self):
        self.server = object.__new__(radius_server.UserManagerRadiusServer)
        self.server._rejection_log_times, self.server._overlimit_attempts = {}, {}
        self.drop_stop = set()
        self.classes = {}

    def acct(self, username, kind, session, rx=0, tx=0):
        if kind == "Start" and (username, session) not in self.classes:
            self.login(username, session)
        pkt = packet.AcctPacket(id=1, secret=b"s", dict=_dict)
        pkt["User-Name"] = username
        pkt["Acct-Status-Type"] = kind
        pkt["Acct-Session-Id"] = session
        pkt["Acct-Input-Octets"] = rx
        pkt["Acct-Output-Octets"] = tx
        if (username, session) in self.classes:
            pkt["Class"] = self.classes[(username, session)]
        pkt.source = (NAS_IP, 1813)
        with patch.object(radius_server, "_send_reply", lambda *a: None):
            self.server.HandleAcctPacket(pkt)

    def login(self, username, session=None):
        pkt = packet.AuthPacket(id=1, secret=b"s", authenticator=os.urandom(16), dict=_dict)
        pkt["User-Name"] = username
        pkt["User-Password"] = pkt.PwCrypt("pw")
        pkt["Calling-Station-Id"] = "10.30.0.9"
        if session:
            pkt["Acct-Session-Id"] = session
        pkt.source = (NAS_IP, 1812)            # the router is the RADIUS client
        replies = []
        with patch.object(radius_server, "_send_reply", side_effect=lambda srv, p, reply: replies.append(reply)), \
                patch.object(radius_server, "_notify_ban_async", lambda *a, **k: None):
            self.server.HandleAuthPacket(pkt)
        reply = replies[-1] if replies else None
        if session and reply is not None and reply.code == packet.AccessAccept and "Class" in reply:
            self.classes[(username, session)] = reply["Class"][0]
        return reply.code if reply is not None else None


def scenario(directory):
    engine = create_engine(f"sqlite:///{directory}/mixed.db", connect_args={"check_same_thread": False})
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

    @contextmanager
    def client(node):
        yield router

    with Session(engine) as db:
        actor = models.AdminUser(username="seller", hashed_password="x", role="seller", tree_path="/1/", balance=0)
        node = models.Node(name="mt", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0", mt_host=NAS_IP)
        package = models.Package(name="mixed", duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package])
        db.flush()
        far = dt.datetime.utcnow() + dt.timedelta(days=10)

        def customer(name, services):
            user = models.User(username=name, owner_admin_id=actor.id, balance=456, purchase_count=len(services),
                               status=models.UserStatus.active)
            db.add(user)
            db.flush()
            ids = []
            for n, kinds in enumerate(services):
                p = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=1000, used_bytes=0,
                                    expire_at=far, status=models.UserStatus.active, max_concurrent_sessions=3)
                db.add(p)
                db.flush()
                for kind in kinds:
                    tag = f"{name}-{n}-{kind}"
                    c = models.Connection(user_id=user.id, purchase_id=p.id, node_id=node.id, enabled=True,
                                          type=getattr(models.ConnectionType, kind), max_concurrent_sessions=1)
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
                ids.append(p.id)
            # sold an hour ago: a router uptime below that proves a reboot since
            hour_ago = dt.datetime.utcnow() - dt.timedelta(hours=1)
            from app.models_reseller_refund import ResellerSaleBasis
            for pid in ids:
                db.query(ResellerSaleBasis).filter_by(purchase_id=pid).update({"created_at": hour_ago})
                db.query(models.Connection).filter_by(purchase_id=pid).update({"created_at": hour_ago})
            db.commit()
            return user.id, ids

        uid, (target, other) = customer("ali", [("wireguard", "openvpn", "l2tp"), ("wireguard", "openvpn")])
        _uid2, (reboot_mixed,) = customer("reza", [("wireguard", "openvpn")])
        _uid3, (reboot_ppp,) = customer("sara", [("openvpn",)])
        _uid4, (partial,) = customer("nima", [("wireguard", "l2tp")])
        aid = actor.id

    def run(pid):
        with Session(engine) as db:
            try:
                return worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
            except HTTPException as exc:
                return exc.status_code, exc.detail

    def components(pid):
        with Session(engine) as db:
            op = db.query(Operation).filter(
                (Operation.purchase_id == pid) | (Operation.retired_purchase_id == pid)).one()
            return {c["type"]: (c["state"], c["wait_reason"]) for c in status.operation(db, op)["components"]}

    def money():
        with Session(engine) as db:
            return (db.get(models.AdminUser, aid).balance,
                    db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count())

    def used(pid):
        with Session(engine) as db:
            p = db.get(models.Purchase, pid)
            return int(p.used_bytes or 0) if p else None

    def open_sessions(name):
        with Session(engine) as db:
            c = db.query(models.Connection).filter_by(ppp_username=name).one()
            return db.query(models.RadiusActiveSession).filter_by(connection_id=c.id).count()

    def reason(outcome):
        return outcome[1]["reason"] if isinstance(outcome[1], dict) else outcome[1]

    def operation_count(pid):
        with Session(engine) as db:
            return db.query(Operation).filter_by(purchase_id=pid).count()

    with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
            patch.dict(os.environ, {"UM_LOCK_DIR": directory}), patch.object(worker.MikrotikClient, "for_node", client), \
            patch.object(worker, "PPP_STOP_WAIT_SECONDS", 1):
        # live sessions on the router, reported by accounting
        for name, sid in (("ali-0-openvpn", "s1"), ("ali-0-l2tp", "s2"), ("ali-1-openvpn", "s3")):
            nas.acct(name, "Start", sid)
        nas.acct("ali-0-openvpn", "Interim-Update", "s1", 60, 40)
        router.active = {"ali-0-openvpn": [("s1", 90, 60)], "ali-0-l2tp": [("s2", 30, 20)],
                         "ali-1-openvpn": [("s3", 0, 0)]}
        check("before: 100 bytes of the target service already counted", used(target), 100)

        # ------------------------------------------------ retransmitted / late accounting (other service)
        nas.acct("ali-1-openvpn", "Stop", "s3", 40, 30)
        nas.acct("ali-1-openvpn", "Stop", "s3", 40, 30)            # the NAS did not get our reply
        # late, after the Stop: sent before it, so never above its final counters
        # (higher ones are a new session reusing the id - test_radius_session_id_reuse)
        nas.acct("ali-1-openvpn", "Interim-Update", "s3", 35, 25)
        check("a retransmitted Stop and an Interim after the Stop count nothing again",
              (used(other), open_sessions("ali-1-openvpn")), (70, 0))
        nas.acct("ali-1-openvpn", "Start", "s4")
        router.active["ali-1-openvpn"] = [("s4", 0, 0)]

        # ------------------------------------------------ the Stop of one session never comes
        with Session(engine) as db:
            check("a mixed WireGuard + OpenVPN + L2TP service is eligible",
                  worker.availability(db, db.get(models.Purchase, target)), None)
        nas.drop_stop = {"ali-0-l2tp"}
        first = run(target)
        check("Stop missing: pending, with that reason", (first[0], first[1]["code"], first[1]["reason"]),
              (409, "reseller_cancellation_pending", "ppp_accounting_stop_missing"))
        check("...each component's state is kept: WG stopped, OpenVPN final, L2TP waiting",
              components(target), {"wireguard": ("usage_captured", None), "openvpn": ("usage_captured", None),
                                   "l2tp": ("blocked", "ppp_accounting_stop_missing")})
        check("...the OpenVPN session's final Stop is counted (100 + 50)", used(target), 150)
        check("...nothing paid, nothing deleted", (money(), router.removed, used(target) is not None),
              ((0, 0), [], True))
        check("...the target's PPP logins are refused", (nas.login("ali-0-openvpn"), nas.login("ali-0-l2tp")),
              (packet.AccessReject, packet.AccessReject))
        check("...the customer's other service still logs in", nas.login("ali-1-openvpn"), packet.AccessAccept)
        with Session(engine) as db:
            wg = db.query(models.Connection).filter_by(wg_peer_name="ali-0-wireguard").one()
            toggles = []
            with patch.object(quota_manager.legacy_remote, "run", lambda *a, **k: toggles.append(a)):
                quota_manager._set_connection_enabled(db, wg, enabled=True)
                quota_manager._set_connection_enabled(db, wg, enabled=False)
            check("...the poller cannot re-enable or toggle the stopped peer", toggles, [])
        check("...its peer is disabled, the other service's peer is not",
              (router.peers["*ali-0-wireguard"]["disabled"], router.peers["*ali-1-wireguard"]["disabled"]),
              (True, False))
        with Session(engine) as db:
            from app.services import user_ops
            p = db.get(models.Purchase, target)
            p.reserved_quota_bytes, p.reserved_duration_days = 1000, 10
            p.reserved_created_at, p.expire_at = dt.datetime.utcnow(), dt.datetime.utcnow() - dt.timedelta(seconds=1)
            toggles = []
            with patch.object(quota_manager.legacy_remote, "run", lambda *a, **k: toggles.append(a)):
                user_ops._maybe_activate_reserved_purchase_renewal(db, p)
            db.rollback()     # observed only; the cancellation's own entitlement check refuses a changed service
        check("a queued renewal activating on it re-enables nothing, and its logins stay refused",
              (toggles, nas.login("ali-0-openvpn"), router.peers["*ali-0-wireguard"]["disabled"]),
              ([], packet.AccessReject, True))
        radius_server.cleanup_stale_radius_sessions(stale_after_minutes=-1)
        check("the 15-minute prune does not remove the waiting session - only the others' stale ones",
              (open_sessions("ali-0-l2tp"), open_sessions("ali-1-openvpn")), (1, 0))
        with Session(engine) as db:
            quota_manager._reconcile_ppp_sessions(db, db.query(models.Node).one(), router)
            db.commit()
        check("...nor does the poller's /ppp/active reconciliation (gone from the router is not a Stop)",
              open_sessions("ali-0-l2tp"), 1)
        with patch.object(settings, "reseller_cancellation_enabled", False):
            check("...the feature switched off while pending does not lift the block",
                  nas.login("ali-0-openvpn"), packet.AccessReject)
            with Session(engine) as db:
                wg = db.query(models.Connection).filter_by(wg_peer_name="ali-0-wireguard").one()
                try:
                    quota_manager._license_restore_holds(db, wg)
                    held = None
                except HTTPException as exc:
                    held = exc.detail
                check("...nor may a licence restore re-enable its peer (flag off too)", held,
                      "reseller_cancellation_pending")
        since = components(target)
        again = run(target)
        check("a resume without the Stop keeps waiting, still unpaid",
              (again[1]["reason"], components(target) == since, money()), ("ppp_accounting_stop_missing", True, (0, 0)))

        # ------------------------------------------------ the Stop arrives late
        nas.acct("ali-0-l2tp", "Stop", "s2", 30, 20)
        done = run(target)
        check("the late Stop: the resume finishes and pays", isinstance(done, dict) and done["state"], "completed")
        expected = int(100000 * (1 - ((200 / 1000) + 0) / 2))
        check("...the refund counts the PPP usage (200 of 1000 bytes) - within a toman of the time share",
              expected - 50 <= done["refund_amount"] <= expected, True)
        check("...credited exactly once", money(), (done["refund_amount"], 1))
        nas.acct("ali-0-l2tp", "Stop", "s2", 30, 20)                # retransmitted after the end
        check("a retry returns the stored result; a late duplicate Stop changes nothing",
              (run(target) == done, money()), (True, (done["refund_amount"], 1)))
        with Session(engine) as db:
            kept = db.get(models.Purchase, other)
            check("the other service, its peer, its PPP credential and the wallet are untouched",
                  (kept is not None, len(kept.connections), router.peers["*ali-1-wireguard"]["disabled"],
                   "ali-1-openvpn" in router.active, db.get(models.User, uid).balance), (True, 2, False, True, 456))
        check("the target's PPP usernames are unknown now (service deleted)",
              nas.login("ali-0-openvpn"), packet.AccessReject)

        # ------------------------------------------------ router reboot
        nas.drop_stop = set()
        nas.acct("reza-0-openvpn", "Start", "s5")
        router.active = {"reza-0-openvpn": [("s5", 10, 10)]}
        router.reboot()
        rebooted = run(reboot_mixed)
        check("reboot: WireGuard continuity refused before anything is touched",
              (reason(rebooted), router.peers["*reza-0-wireguard"]["disabled"],
               operation_count(reboot_mixed)), ("wg_usage_continuity_unverified", False, 0))
        nas.acct("sara-0-openvpn", "Start", "s6")
        lost = run(reboot_ppp)
        check("reboot: a PPP session that vanished without a Stop - refused before anything is touched, unpaid",
              (reason(lost), money()[1], operation_count(reboot_ppp)), ("ppp_session_unverified", 1, 0))
        router.uptime = "52w"

        # ------------------------------------------------ partial failure, then resume
        router.fail_remove = 1
        failed = run(partial)
        check("the WireGuard delete fails: pending with the reason; L2TP already final",
              (failed[1]["reason"], components(partial)),
              ("wg_refund_delete_unconfirmed", {"wireguard": ("usage_captured", None),
                                                "l2tp": ("usage_captured", None)}))
        resumed = run(partial)
        check("resumed: the same operation finishes, the peer is deleted once, one more credit",
              (resumed["state"], router.removed.count("nima-0-wireguard"), money()[1]), ("completed", 1, 2))
    engine.dispose()


with TemporaryDirectory() as directory:
    scenario(directory)
if failures:
    print(f"{len(failures)} FAILED")
    sys.exit(1)
print("all checks passed")
