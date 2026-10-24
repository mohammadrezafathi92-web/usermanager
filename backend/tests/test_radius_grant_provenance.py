"""Class-bound accounting: immutable credential generation and final proof.

Real pyrad handler, local SQLite only. UM_REVIEW_SOURCE_ROOT allows this
regression to run against an immutable pre-fix source snapshot.
"""
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(os.environ.get("UM_REVIEW_SOURCE_ROOT", str(Path(__file__).resolve().parents[1])))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("RADIUS_ENABLED", "false")
os.environ.setdefault("SECRET_KEY", "local-review-test-only-0123456789abcdef")
import _no_network
_no_network.install()

from pyrad import packet
from pyrad.dictionary import Dictionary
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from app import models
from app.config import settings
from app.database import Base
from app.services import radius_server as radius
from app.services import reseller_refund_settlement as settlement

failures = []
dictionary = Dictionary(radius.DICT_PATH)


def check(label, got, expected):
    if got != expected:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")
    else:
        print(f"PASS {label}")


def scenario():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    with factory() as db:
        node = models.Node(name="local-test", type=models.NodeType.mikrotik, mt_host="10.20.0.1")
        user = models.User(username="original", status=models.UserStatus.active)
        other = models.User(username="other", status=models.UserStatus.active)
        db.add_all([node, user, other]); db.flush()
        first = models.Purchase(user_id=user.id, quota_bytes=2**50, status=models.UserStatus.active)
        second = models.Purchase(user_id=other.id, quota_bytes=2**50, status=models.UserStatus.active)
        db.add_all([first, second]); db.flush()
        conn = models.Connection(user_id=user.id, node_id=node.id, purchase_id=first.id,
                                 type=models.ConnectionType.openvpn, ppp_username="test-ppp",
                                 ppp_password="pw", enabled=True)
        db.add(conn); db.commit()
        cid, pid, other_pid, nid, uid = conn.id, first.id, second.id, node.id, user.id
    server = object.__new__(radius.UserManagerRadiusServer)
    server._rejection_log_times, server._overlimit_attempts = {}, {}
    replies = []

    def login():
        pkt = packet.AuthPacket(id=7, secret=b"s", authenticator=os.urandom(16), dict=dictionary)
        pkt["User-Name"], pkt["User-Password"] = "test-ppp", pkt.PwCrypt("pw")
        pkt["Acct-Session-Id"] = "same-sid"
        pkt.source = ("10.20.0.1", 1812)
        server._handle_auth_packet(pkt, "enforced")
        return replies[-1]["Class"][0]

    def acct(nonce, kind, rx=0, tx=0, *, missing=False):
        pkt = packet.AcctPacket(id=3, secret=b"s", dict=dictionary)
        pkt["User-Name"], pkt["Acct-Status-Type"], pkt["Acct-Session-Id"] = "test-ppp", kind, "same-sid"
        pkt["Class"] = nonce
        if not missing:
            pkt["Acct-Input-Octets"], pkt["Acct-Output-Octets"] = rx, tx
        pkt.source = ("10.20.0.1", 1813)
        server.HandleAcctPacket(pkt)

    def proof():
        with factory() as db:
            step = SimpleNamespace(connection_id=cid, ppp_unverified_at=None, ppp_seen_sessions="{}")
            return settlement.ppp_stop_missing(db, step)

    with patch.object(settings, "reseller_cancellation_enabled", True), \
         patch.object(radius, "SessionLocal", factory), \
         patch.object(radius, "_send_reply", lambda _s, _p, reply: replies.append(reply)), \
         patch.object(radius, "_enforce_user_limits", lambda *a: None), \
         patch.object(radius, "_enforce_purchase_limits", lambda *a: None):
        nonce = login()
        with factory() as db:
            old = db.get(models.Connection, cid)
            db.delete(old); db.flush()
            db.add(models.Connection(id=cid, user_id=db.get(models.Purchase, other_pid).user_id,
                                     node_id=nid, purchase_id=other_pid, type=models.ConnectionType.openvpn,
                                     ppp_username="test-ppp", ppp_password="pw", enabled=True))
            db.commit()
        acct(nonce, "Stop", 111, 222)
        with factory() as db:
            check("reused id and identical credential cannot bill another owner", db.get(models.Purchase, other_pid).used_bytes, 0)
            grant = db.query(models.RadiusAuthGrant).filter_by(nonce=nonce.decode()).one()
            check("old grant keeps original purchase provenance", grant.purchase_id, pid)
            check("retired generation cannot supply or block current grant evidence",
                  settlement.ppp_sessions_unverified(db, db.get(models.Connection, cid), []), False)

        # Re-creating even the SAME owner's identical credential is not a
        # legitimate in-place migration and must get a new generation.
        fresh_nonce = login()
        with factory() as db:
            old = db.get(models.Connection, cid)
            owner_id = old.user_id
            db.delete(old); db.flush()
            db.add(models.Connection(id=cid, user_id=owner_id, node_id=nid, purchase_id=other_pid,
                                     type=models.ConnectionType.openvpn, ppp_username="test-ppp",
                                     ppp_password="pw", enabled=True))
            db.commit()
        acct(fresh_nonce, "Stop", 12, 34)
        with factory() as db:
            check("same owner credential recreation still cannot inherit old grant", db.get(models.Purchase, other_pid).used_bytes, 0)

        # A genuine new credential generation works, and moving this SAME row
        # between purchases preserves the grant instead of dropping its usage.
        nonce = login()
        with factory() as db:
            db.get(models.Connection, cid).purchase_id = pid
            db.commit()
        acct(nonce, "Interim-Update", 100, 200)
        with factory() as db:
            check("same-row purchase migration still counts its traffic", db.get(models.Purchase, pid).used_bytes, 300)
        acct(nonce, "Stop", missing=True)
        check("Stop without final counters is not settlement proof", proof() is not None, True)
        with factory() as db:
            grant = db.query(models.RadiusAuthGrant).filter_by(nonce=nonce.decode()).one()
            check("missing final counters do not end grant", grant.state != "stopped", True)
        acct(nonce, "Stop", 50, 200)
        check("regressing final counter is not settlement proof", proof() is not None, True)
        acct(nonce, "Stop", 150, 250)
        check("corrected complete Stop resolves final proof", proof(), None)
        with factory() as db:
            check("corrected Stop counts only remaining bytes", db.get(models.Purchase, pid).used_bytes, 400)
        acct(nonce, "Stop", 150, 250)
        with factory() as db:
            check("retransmitted corrected Stop is exact once", db.get(models.Purchase, pid).used_bytes, 400)
            db.get(models.Connection, cid).purchase_id = other_pid
            db.commit()
        acct(nonce, "Stop", 150, 250)
        with factory() as db:
            grant = db.query(models.RadiusAuthGrant).filter_by(nonce=nonce.decode()).one()
            check("ended grant provenance does not follow later purchase migration", grant.purchase_id, pid)
            db.get(models.Connection, cid).purchase_id = pid
            db.commit()

        zero = login()
        acct(zero, "Stop", 0, 0)
        check("literal zero final counters are valid for unused session", proof(), None)

        wrapped = login()
        acct(wrapped, "Interim-Update", 2**32 - 10, 0)
        acct(wrapped, "Stop", 20, 0)    # no Gigawords: the wrap is unproven
        check("wrapped final counter without high word cannot settle", proof() is not None, True)
        pkt = packet.AcctPacket(id=4, secret=b"s", dict=dictionary)
        pkt["User-Name"], pkt["Acct-Status-Type"], pkt["Acct-Session-Id"] = "test-ppp", "Stop", "same-sid"
        pkt["Class"] = wrapped
        pkt["Acct-Input-Octets"], pkt["Acct-Input-Gigawords"], pkt["Acct-Output-Octets"] = 20, 1, 0
        pkt.source = ("10.20.0.1", 1813)
        server.HandleAcctPacket(pkt)
        check("correct explicit high word resolves the wrapped final counter", proof(), None)

        parser = getattr(radius, "_verified_counter", lambda *a: 0)
        for label, value in (("negative", -1), ("overflow", 2**32), ("boolean", True), ("text", "12")):
            check(f"malformed {label} low word is not a zero sample",
                  parser({"low": [value]}, "low", "high"), None)
        check("duplicate final attributes are not a verified sample", parser({"low": [1, 2]}, "low", "high"), None)
        check("malformed high word is not ignored", parser({"low": [1], "high": [-1]}, "low", "high"), None)

        credential = login()
        with factory() as db:
            before = db.get(models.Purchase, pid).used_bytes
            db.get(models.Connection, cid).ppp_password = "changed-after-accept"
            db.commit()
        check("password rotation cannot hide pending accepted session", proof() is not None, True)
        acct(credential, "Stop", 700, 900)
        with factory() as db:
            check("already accepted session still accounts after password rotation", db.get(models.Purchase, pid).used_bytes, before + 1600)
            grant = db.query(models.RadiusAuthGrant).filter_by(nonce=credential.decode()).one()
            check("its valid Stop resolves the rotated credential session", grant.state, "stopped")
            db.get(models.Connection, cid).ppp_password = "pw"
            db.commit()
        ownership = login()
        with factory() as db:
            before = db.get(models.Purchase, pid).used_bytes
            db.get(models.Connection, cid).user_id = uid
            db.commit()
        acct(ownership, "Stop", 333, 444)
        with factory() as db:
            check("same-row different customer does not inherit prior usage", db.get(models.Purchase, pid).used_bytes, before)
        check("ownership drift cannot hide pending grant from cancellation", proof() is not None, True)
    engine.dispose()


scenario()
if failures:
    raise SystemExit(f"{len(failures)} regressions failed")
print("all checks passed")
