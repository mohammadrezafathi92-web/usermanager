"""The panel's per-service enable/disable switch reaches the router.

Run:  python3 backend/tests/test_connection_manual_toggle.py

Bug (2026-11-03, customer report): pressing "disable" on a WireGuard
service only flipped connections.enabled in the database. The router peer
stayed enabled (the customer stayed connected) and the next poll's
reconcile saw "quota/expiry fine but disabled" and switched it back on.
Now the switch is pushed to the node, and an administrator's disable is an
intent no automatic path (poll reconcile, renewal, account enable) undoes.
"""
from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("SECRET_KEY", "connection-manual-toggle-test-only-0123456789")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import deps, models
from app.database import get_db
from app.routers import users as users_router
from app.services import legacy_remote, quota_manager, user_ops

failures: list[str] = []


def check(label, got, expected):
    if callable(got):
        try:
            got = got()
        except Exception as exc:  # noqa: BLE001 - one FAIL, not an abort
            got = f"raised {type(exc).__name__}: {exc}"
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


router_state: dict[str, bool] = {}
calls: list[tuple] = []
router_reachable = {"ok": True}


def fake_run(node, block, **kwargs):
    calls.append((block, kwargs.get("peer_name"), kwargs.get("enabled")))
    if block != "wg_set_enabled":
        return True
    if not router_reachable["ok"]:
        return False
    router_state[kwargs["peer_name"]] = kwargs["enabled"]
    return True


legacy_remote.run = fake_run

engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
models.Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
root = models.AdminUser(username="root", hashed_password="x", is_superadmin=True, tree_path="/1/")
node = models.Node(name="com", type=models.NodeType.mikrotik, mt_host="10.0.0.1", mt_wireguard_interface="wireguard-com")
db.add_all([root, node]); db.flush()
now = dt.datetime.utcnow()
user = models.User(username="u1", owner_admin_id=root.id, status=models.UserStatus.active)
db.add(user); db.flush()
purchase = models.Purchase(user_id=user.id, quota_bytes=10 * 1024 ** 3, used_bytes=0,
                           expire_at=now + dt.timedelta(days=10), status=models.UserStatus.active)
db.add(purchase); db.flush()
conn = models.Connection(user_id=user.id, node_id=node.id, purchase_id=purchase.id, type=models.ConnectionType.wireguard,
                         wg_peer_name="user1", enabled=True)
db.add(conn); db.commit()
router_state["user1"] = True

app = FastAPI()
app.include_router(users_router.router)
app.dependency_overrides[get_db] = lambda: db
app.dependency_overrides[deps.get_current_admin] = lambda: db.get(models.AdminUser, root.id)
client = TestClient(app)


def toggle(enabled):
    return client.put(f"/api/users/{user.id}/connections/{conn.id}", json={"enabled": enabled})


def reload():
    db.expire_all()
    return db.get(models.Connection, conn.id)


r = toggle(False)
check("disable answers 200", r.status_code, 200)
check("disable is pushed to the router peer", router_state.get("user1"), False)
check("panel shows it disabled", reload().enabled, False)

quota_manager._reconcile_connection_enabled_state(db, dt.datetime.utcnow()); db.commit()
check("the poll reconcile does not switch it back on", (reload().enabled, router_state["user1"]), (False, False))

user_ops.reconcile_user_connections(db, db.get(models.User, user.id)); db.commit()
check("an account-wide enable/renewal does not switch it back on", (reload().enabled, router_state["user1"]),
      (False, False))

r = toggle(True)
check("enable answers 200", r.status_code, 200)
check("enable is pushed to the router peer", router_state.get("user1"), True)
check("panel shows it enabled", reload().enabled, True)
quota_manager._reconcile_connection_enabled_state(db, dt.datetime.utcnow()); db.commit()
check("after enable the reconcile leaves it on", (reload().enabled, router_state["user1"]), (True, True))

router_reachable["ok"] = False
r = toggle(False)
check("router unreachable on disable -> 502, not a silent success", r.status_code, 502)
check("... and the panel does not claim it is off", reload().enabled, True)
router_reachable["ok"] = True
quota_manager._reconcile_connection_enabled_state(db, dt.datetime.utcnow()); db.commit()
check("... the intent is kept and the next poll disables it", (reload().enabled, router_state["user1"]),
      (False, False))
toggle(True)

p = db.get(models.Purchase, purchase.id)
p.expire_at = dt.datetime.utcnow() - dt.timedelta(days=1); db.commit()
quota_manager._reconcile_connection_enabled_state(db, dt.datetime.utcnow()); db.commit()
check("expired service is cut by the poll", router_state["user1"], False)
r = toggle(True)
check("enable on an expired service does not open the peer", (r.status_code, router_state["user1"]), (200, False))

if failures:
    print(f"\n{len(failures)} FAILED")
    sys.exit(1)
print("\nall manual toggle checks passed")
