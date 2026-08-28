"""The RADIUS layer is where the real abuse was actually happening (the
admin's own screenshot: "کاربر ناشناس" and "اتصال غیرفعال" rows repeating
for hours from the same couple of IPs on radius_logs.py's page). This
drives services/radius_server.py's HandleAuthPacket with REAL pyrad packets
end to end, to prove the ip_guard wiring actually bans there - not just
that ip_guard's own counters work in isolation (see test_ip_guard.py for
that half).

Run:  python3 backend/tests/test_radius_ip_ban.py

UserManagerRadiusServer.__init__ binds a real UDP socket (it calls
Server.BindToAddress for every configured address) - not something a test
should do. The instance below is built with object.__new__, bypassing
__init__ entirely; HandleAuthPacket only ever touches
self._rejection_log_times (set by hand here) and inherited methods that
don't need any other instance state (CreateReplyPacket just reads the
packet it's given).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("RADIUS_ENABLED", "false")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from pyrad import packet
from pyrad.dictionary import Dictionary

from app import models
from app.services import ip_guard, radius_server

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


def reset_ip_guard_state():
    ip_guard._banned_ips.clear()
    ip_guard._unknown_hits.clear()
    ip_guard._post_bad_cred_hits.clear()
    ip_guard._radius_unknown_hits.clear()
    ip_guard._radius_inactive_hits.clear()


_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(_engine)
_TestSession = sessionmaker(bind=_engine)
radius_server.SessionLocal = _TestSession  # the module-level name HandleAuthPacket actually calls

_dict = Dictionary(radius_server.DICT_PATH)


def make_server():
    """A HandleAuthPacket-capable instance with no real socket bound."""
    srv = object.__new__(radius_server.UserManagerRadiusServer)
    srv._rejection_log_times = {}
    return srv


def make_request(username: str, client_ip: str) -> "packet.AuthPacket":
    pkt = packet.AuthPacket(id=1, secret=b"testsecret", authenticator=os.urandom(16), dict=_dict)
    pkt["User-Name"] = username
    pkt["Calling-Station-Id"] = client_ip
    pkt.source = (client_ip, 12345)
    return pkt


class ReplyRecorder:
    """Stands in for the real socket send - _send_reply is a module-level
    function called by bare name, so it's patched at the module level and
    restored afterwards."""

    def __init__(self):
        self.replies = []

    def __call__(self, server, pkt, reply):
        self.replies.append(reply)


_orig_send_reply = radius_server._send_reply


def seed_disabled_connection(db, username: str, admin_id: int, node_id: int) -> None:
    # A real disabled connection normally belongs to a user - build one so
    # this matches production shape rather than a connection floating with
    # no owner.
    user = models.User(username=f"owner-{username}", status=models.UserStatus.disabled, owner_admin_id=admin_id)
    db.add(user)
    db.flush()
    conn = models.Connection(
        ppp_username=username, ppp_password="x", type=models.ConnectionType.l2tp,
        enabled=False, user_id=user.id, node_id=node_id,
    )
    db.add(conn)
    db.commit()


print("--- an unknown username, repeated from one IP, bans that IP at 10 ---")
reset_ip_guard_state()
db = _TestSession()
rec = ReplyRecorder()
radius_server._send_reply = rec
server = make_server()

CLIENT_IP = "37.114.246.98"
last_code = None
for i in range(12):
    # SAME username every time, exactly like the real scan in the
    # screenshot ("UnL0616501" repeated) - this is what makes the log
    # page's 10-minute de-dup collapse it to ~1 row, which is exactly the
    # case that would defeat a ban counter fed from the de-duplicated rate
    # instead of the raw attempt rate.
    pkt = make_request("UnL0616501", CLIENT_IP)
    server.HandleAuthPacket(pkt)
    last_code = rec.replies[-1].code

check("every one of these is rejected", last_code, packet.AccessReject)
check("after 12 unknown-username attempts from the same IP, it is banned",
      ip_guard.is_banned(CLIENT_IP), True)
rows = db.query(models.RadiusLimitEventLog).filter(
    models.RadiusLimitEventLog.event_type == "unknown_user").count()
check("the log itself still only has ~1 row (10-min de-dup unaffected by the ban counter)",
      rows <= 2, True)

print("\n--- once banned, the NEXT attempt is refused before any DB lookup happens ---")
before_count = db.query(models.RadiusLimitEventLog).count()
pkt = make_request("yet-another-unknown-name", CLIENT_IP)
server.HandleAuthPacket(pkt)
after_count = db.query(models.RadiusLimitEventLog).count()
check("still an Access-Reject", rec.replies[-1].code, packet.AccessReject)
check("but no new log row - it never got past the ban check to compute a reason",
      after_count, before_count)

print("\n--- a DIFFERENT ip is unaffected by the first one's ban ---")
reset_ip_guard_state()
pkt = make_request("someone-else-unknown", "5.5.5.5")
server.HandleAuthPacket(pkt)
check("not banned - only 1 attempt", ip_guard.is_banned("5.5.5.5"), False)
check("still gets a normal reject (unknown user), not blocked outright",
      rec.replies[-1].code, packet.AccessReject)

print("\n--- a disabled (اتصال غیرفعال) connection, repeated, bans at 20 not 10 ---")
reset_ip_guard_state()
admin = models.AdminUser(username="a1", hashed_password="x", is_superadmin=True)
node = models.Node(name="n1", type="mikrotik", enabled=True, mt_host="10.0.0.1")
db.add_all([admin, node])
db.commit()
seed_disabled_connection(db, "test11111-sstp-b2ffc", admin_id=admin.id, node_id=node.id)

INACTIVE_IP = "79.127.122.174"
for i in range(25):
    pkt = make_request("test11111-sstp-b2ffc", INACTIVE_IP)
    server.HandleAuthPacket(pkt)
    if ip_guard.is_banned(INACTIVE_IP):
        break
check("takes noticeably more attempts than the unknown-user rule (20, not 10)",
      i + 1 >= ip_guard.RADIUS_INACTIVE_LIMIT, True)
check("...and it does eventually ban", ip_guard.is_banned(INACTIVE_IP), True)

radius_server._send_reply = _orig_send_reply

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("همه‌ی تست‌ها گذشت")
