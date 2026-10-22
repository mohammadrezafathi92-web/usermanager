"""The licence heartbeat carries three aggregate counts - and nothing that
identifies a customer. A customer with several connections counts once;
sessions are counted apart from customers; unreadable counts are omitted,
never sent as zero.

Run:  python3 backend/tests/test_license_stats.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ.setdefault("SECRET_KEY", "license-stats-local-test-only")

from app import models  # noqa: E402
from app.config import settings  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.services import license_state  # noqa: E402

failures: list[str] = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


Base.metadata.create_all(engine)
with SessionLocal() as db:
    node = models.Node(name="n", type=models.NodeType.mikrotik)
    db.add(node); db.flush()
    many = models.User(username="alice-many-conns", telegram_id=555001)
    idle = models.User(username="bob-idle", telegram_id=555002)
    two_sessions = models.User(username="carol-two-sessions")
    db.add_all([many, idle, two_sessions]); db.flush()
    c = lambda u, t, online=False: models.Connection(user_id=u.id, node_id=node.id, type=t, online=online)
    a1, a2, a3 = c(many, models.ConnectionType.xray, True), c(many, models.ConnectionType.openvpn), c(many, models.ConnectionType.wireguard, True)
    b1 = c(idle, models.ConnectionType.l2tp)
    k1, k2 = c(two_sessions, models.ConnectionType.l2tp), c(two_sessions, models.ConnectionType.openvpn)
    db.add_all([a1, a2, a3, b1, k1, k2]); db.flush()
    db.add_all([models.RadiusActiveSession(connection_id=a2.id, session_id="s1"),
                models.RadiusActiveSession(connection_id=k1.id, session_id="s2"),
                models.RadiusActiveSession(connection_id=k2.id, session_id="s3")])
    db.commit()

stats = license_state.aggregate_stats()
check("three customer accounts - not six connections", stats["customer_accounts"], 3)
check("two online accounts - alice once despite three online connections", stats["online_customer_accounts"], 2)
check("sessions/connections counted apart: 3 RADIUS + 2 flagged online", stats["online_sessions"], 5)
check("exactly these keys, versioned and time-stamped",
      sorted(stats), ["customer_accounts", "online_customer_accounts", "online_sessions", "sampled_at", "version"])


def heartbeat_body():
    sent = {}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"revoked": false, "lock_scope": "panel_only", "activated_at": null}'

    def urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode())
        return Resp()

    body = base64.urlsafe_b64encode(json.dumps({"id": "lic_test", "v": 1}).encode()).decode().rstrip("=")
    with patch.object(settings, "license_key", f"NETCIP1.{body}.c2ln"), \
            patch.object(settings, "license_server_url", "https://license.invalid"), \
            patch.object(settings, "license_master_install", False), \
            patch.object(license_state, "_save_state", lambda s: None), \
            patch.object(license_state.licensing, "hardware_fingerprint", lambda: "fp"), \
            patch.object(license_state, "refresh", lambda: None), \
            patch("urllib.request.urlopen", urlopen):
        license_state.heartbeat()
    return sent["body"]


body = heartbeat_body()
check("the heartbeat carries the counts", body["stats"]["customer_accounts"], 3)
check("the rest of the heartbeat is unchanged",
      sorted(k for k in body if k != "stats"), ["activated_at", "fingerprint", "license_id", "panel_version"])
raw = json.dumps(body, ensure_ascii=False)
check("no username, Telegram id or session id leaves the panel",
      any(x in raw for x in ("alice", "bob", "carol", "555001", "555002", '"s1"', "s2")), False)

with patch("app.database.SessionLocal", side_effect=RuntimeError("database locked")):
    check("unreadable counts -> omitted (None), never zeros", license_state.aggregate_stats(), None)
    check("...and the heartbeat goes out without them", "stats" in heartbeat_body(), False)

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
