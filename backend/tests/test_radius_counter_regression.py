"""A Class-bound Interim whose counter went DOWN is durable ambiguity.

Interim 5000 -> Interim 100 -> Stop 6000 used to end as an ordinary final
proof: the high-water mark hid the decrease and the Stop cleared every
doubt. A counter wrap without Gigawords, a NAS counter reset, and a merely
reordered (delayed) Interim all look like this; none of them is proved and
no missing byte amount is guessed. The decrease is remembered on the grant
and no later report clears it, so both settlement proof helpers fail closed
(a reordered packet can therefore conservatively leave a cancellation
pending). Missing/invalid final Stop recovery is a different, recoverable
gap and must keep working. Residual boundary (existing contract, not
universal reset detection): a lower FINAL sample is that recoverable gap,
so Interim 5000 -> Stop 100 -> Stop 6000 trusts the later corrected final.

Real pyrad handler, fresh in-memory SQLite per case, no network.
UM_REVIEW_SOURCE_ROOT runs it against an immutable pre-fix source snapshot.
"""
import os
import sys
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
from sqlalchemy.orm import sessionmaker
from app import models
from app.config import settings
from app.database import Base
from app.services import radius_server as radius
from app.services import reseller_refund_settlement as settlement

failures = []
dictionary = Dictionary(radius.DICT_PATH)
REGRESSED = "ppp_accounting_counter_regressed"


def check(label, got, expected):
    if got != expected:
        failures.append(label)
        print(f"FAIL {label}: got {got!r}, expected {expected!r}")
    else:
        print(f"PASS {label}")


class World:
    """One connection on one trusted node, the real auth/accounting handlers."""

    def __init__(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False)
        with self.factory() as db:
            node = models.Node(name="local-test", type=models.NodeType.mikrotik, mt_host="10.20.0.1")
            user = models.User(username="owner", status=models.UserStatus.active)
            db.add_all([node, user]); db.flush()
            purchase = models.Purchase(user_id=user.id, quota_bytes=2**50, status=models.UserStatus.active)
            db.add(purchase); db.flush()
            conn = models.Connection(user_id=user.id, node_id=node.id, purchase_id=purchase.id,
                                     type=models.ConnectionType.openvpn, ppp_username="test-ppp",
                                     ppp_password="pw", enabled=True)
            db.add(conn); db.commit()
            self.cid, self.pid = conn.id, purchase.id
        self.server = object.__new__(radius.UserManagerRadiusServer)
        self.server._rejection_log_times, self.server._overlimit_attempts = {}, {}
        self.replies, self.packet_id = [], 0

    def login(self):
        pkt = packet.AuthPacket(id=7, secret=b"s", authenticator=os.urandom(16), dict=dictionary)
        pkt["User-Name"], pkt["User-Password"] = "test-ppp", pkt.PwCrypt("pw")
        pkt["Acct-Session-Id"] = "same-sid"
        pkt.source = ("10.20.0.1", 1812)
        self.server._handle_auth_packet(pkt, "enforced")
        return self.replies[-1]["Class"][0]

    def acct(self, nonce, kind, rx=None, tx=None):
        self.packet_id = (self.packet_id + 1) % 256
        pkt = packet.AcctPacket(id=self.packet_id, secret=b"s", dict=dictionary)
        pkt["User-Name"], pkt["Acct-Status-Type"], pkt["Acct-Session-Id"] = "test-ppp", kind, "same-sid"
        pkt["Class"] = nonce
        if rx is not None:
            pkt["Acct-Input-Octets"], pkt["Acct-Output-Octets"] = rx, tx
        pkt.source = ("10.20.0.1", 1813)
        self.server.HandleAcctPacket(pkt)

    def proof(self):
        with self.factory() as db:
            step = SimpleNamespace(connection_id=self.cid, ppp_unverified_at=None, ppp_seen_sessions="{}")
            return settlement.ppp_stop_missing(db, step)

    def precheck(self, router_sessions):
        with self.factory() as db:
            return settlement.ppp_sessions_unverified(db, db.get(models.Connection, self.cid), router_sessions)

    def used(self):
        with self.factory() as db:
            return db.get(models.Purchase, self.pid).used_bytes

    def grant(self, nonce):
        with self.factory() as db:
            g = db.query(models.RadiusAuthGrant).filter_by(nonce=nonce.decode()).one()
            return SimpleNamespace(state=g.state, unverified=g.accounting_unverified,
                                   regressed=getattr(g, "counter_regressed_at", None) is not None)


def run(fn):
    world = World()
    with patch.object(settings, "reseller_cancellation_enabled", True), \
         patch.object(radius, "SessionLocal", world.factory), \
         patch.object(radius, "_send_reply", lambda _s, _p, reply: world.replies.append(reply)), \
         patch.object(radius, "_enforce_user_limits", lambda *a: None), \
         patch.object(radius, "_enforce_purchase_limits", lambda *a: None):
        fn(world)
    world.engine.dispose()


def reproduced_sequence(w):
    # The exact independently reproduced sequence.
    nonce = w.login()
    w.acct(nonce, "Start", 0, 0)
    w.acct(nonce, "Interim-Update", 5000, 0)
    w.acct(nonce, "Interim-Update", 100, 0)
    check("decreasing Interim adds nothing (high-water dedup kept)", w.used(), 5000)
    check("decreasing Interim is recorded durably", w.grant(nonce).regressed, True)
    check("precheck refuses an open session with a regressed counter", w.precheck(["same-sid"]), True)
    w.acct(nonce, "Stop", 6000, 0)
    g = w.grant(nonce)
    check("later Stop still ends the session", g.state, "stopped")
    check("later Stop counts only bytes above the high-water mark (no guessed wrap)", w.used(), 6000)
    check("later complete Stop does not clear the regression", g.regressed, True)
    check("settlement proof refuses after the catch-up Stop", w.proof(), REGRESSED)
    check("precheck refuses an ended session with a regressed counter", w.precheck([]), True)
    w.acct(nonce, "Stop", 6000, 0)
    w.acct(nonce, "Interim-Update", 7000, 0)
    check("stopped-grant replay and late Interim still ignored", w.used(), 6000)
    check("replays do not clear the regression either", w.proof(), REGRESSED)


def one_direction(w):
    nonce = w.login()
    w.acct(nonce, "Interim-Update", 100, 500)
    w.acct(nonce, "Interim-Update", 200, 400)
    check("one direction decreasing is a regression", w.grant(nonce).regressed, True)
    w.acct(nonce, "Stop", 300, 600)
    check("one-direction regression blocks settlement proof", w.proof(), REGRESSED)
    check("its usage is per-direction high-water, nothing guessed", w.used(), 900)


def monotonic_baseline(w):
    nonce = w.login()
    w.acct(nonce, "Start", 0, 0)
    w.acct(nonce, "Interim-Update", 100, 50)
    w.acct(nonce, "Interim-Update", 100, 50)    # retransmitted / equal: not a decrease
    w.acct(nonce, "Interim-Update", 300, 80)
    check("equal or growing counters are not a regression", w.grant(nonce).regressed, False)
    check("monotonic open session passes precheck", w.precheck(["same-sid"]), False)
    w.acct(nonce, "Stop", 400, 100)
    check("monotonic session settles", w.proof(), None)
    check("monotonic usage exact", w.used(), 500)


def missing_stop_recovery(w):
    nonce = w.login()
    w.acct(nonce, "Interim-Update", 100, 200)
    w.acct(nonce, "Stop")                       # no final counters
    g = w.grant(nonce)
    check("missing final counters keep the grant unended", g.state != "stopped", True)
    check("missing final counters are not a counter regression", g.regressed, False)
    check("missing final counters are not settlement proof", w.proof() is not None, True)
    w.acct(nonce, "Stop", 150, 250)
    check("corrected complete Stop still resolves final proof", w.proof(), None)
    check("corrected Stop counts only remaining bytes", w.used(), 400)


def regressing_stop_recovery(w):
    # A regressing FINAL sample stays the existing recoverable evidence gap
    # (current reviewed semantics; any change is a separate decision).
    nonce = w.login()
    w.acct(nonce, "Interim-Update", 100, 200)
    w.acct(nonce, "Stop", 50, 200)
    check("regressing Stop is not settlement proof", w.proof() is not None, True)
    w.acct(nonce, "Stop", 150, 250)
    check("corrected Stop after a regressing Stop still resolves", w.proof(), None)


for case in (reproduced_sequence, one_direction, monotonic_baseline, missing_stop_recovery,
             regressing_stop_recovery):
    run(case)
if failures:
    raise SystemExit(f"{len(failures)} regressions failed")
print("all counter-regression checks passed")
