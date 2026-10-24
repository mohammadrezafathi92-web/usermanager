"""A rejected read-only cancellation precheck is not a cancellation hold.

Fake nodes only. Tests the real worker, quota switch and authenticated-route
fence, plus an uncertain first WG write: that intent MUST remain held.
UM_REFUND_TEST_SOURCE may name an immutable backend snapshot for fail-before.
"""
import datetime as dt
import os
from pathlib import Path
import sys
import tempfile
from contextlib import contextmanager
from unittest.mock import patch

_tmp = tempfile.TemporaryDirectory(prefix="um-precheck-holds-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmp.name}/app.db")
os.environ.setdefault("SECRET_KEY", "test-only-precheck-holds-01234567890123456789")
os.environ.setdefault("RADIUS_ENABLED", "false")
sys.path.insert(0, os.environ.get("UM_REFUND_TEST_SOURCE", str(Path(__file__).resolve().parents[1])))

from fastapi import HTTPException
from sqlalchemy.orm import Session, sessionmaker
from app import models, database, schemas
from app.routers import users
from app.database import Base, engine
from app.config import settings
from app.models_reseller_refund import ResellerRefundOperation as Operation, ResellerRefundStep as Step
from app.services import quota_manager, reseller_refund_worker as worker
from app.services import reseller_refund_fence as fence, reseller_refund_block as block
from app.services.adapter_base import AdapterError
from app.services.mikrotik_client import MikrotikError
from app.services.reseller_refund import bind_sale
from app.services.reseller_refund_settlement import prepare, block_ppp

Base.metadata.create_all(engine)
factory = sessionmaker(bind=engine, autoflush=False)
failures = []


def check(label, actual, expected):
    if actual == expected:
        print("PASS", label)
    else:
        failures.append(label)
        print("FAIL", label, "got", repr(actual), "expected", repr(expected))


class Router:
    sessions = []
    unreadable = False

    def list_peers(self, interface):
        return [{".id": "*1", "interface": "wg0", "comment": "peer", "public-key": "key",
                 "allowed-address": "10.0.0.2/32", "disabled": False, "rx": "0", "tx": "0"}]

    def get_system_resources(self):
        return {"uptime": "52w"}

    def ppp_session_ids(self, username):
        if self.unreadable:
            raise MikrotikError("test-only node unavailable")
        return self.sessions


router = Router()


@contextmanager
def client(node):
    yield router


def fixture(label, *, ppp=True):
    with factory() as db:
        actor = models.AdminUser(username="seller-" + label, hashed_password="x", role="seller", tree_path="/1/", balance=0)
        node = models.Node(name="mt-" + label, type=models.NodeType.mikrotik, mt_host="10.0.0.1", mt_wireguard_interface="wg0")
        package = models.Package(name=label, duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package]); db.flush()
        user = models.User(username=label, owner_admin_id=actor.id, status=models.UserStatus.active, purchase_count=1)
        db.add(user); db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=1000, used_bytes=0,
                                   expire_at=dt.datetime.utcnow() + dt.timedelta(days=10), status=models.UserStatus.active)
        db.add(purchase); db.flush()
        wg = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id, enabled=True,
                               type=models.ConnectionType.wireguard, wg_peer_name="peer", wg_public_key="key",
                               wg_client_address="10.0.0.2/32", created_at=dt.datetime.utcnow() - dt.timedelta(hours=1))
        db.add(wg); db.flush()
        pc = None
        if ppp:
            pc = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id, enabled=True,
                                   type=models.ConnectionType.openvpn, ppp_username=label + "-ovpn", ppp_password="pw")
            db.add(pc); db.flush()
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
        sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id, purchase_id=purchase.id)
        db.add_all([debit, sale]); db.flush()
        bind_sale(db, purchase, debit, package, sale_entry=sale, purchase_count_delta=1)
        result = (purchase.id, user.id, actor.id, wg.id, pc.id if pc else None)
        db.commit()
        return result


def run(row):
    pid, _uid, aid, _wg, _ppp = row
    with factory() as db:
        try:
            return worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
        except HTTPException as exc:
            return exc.detail.get("reason", exc.detail) if isinstance(exc.detail, dict) else exc.detail


def ordinary_mutation(user_id):
    pass


ordinary_mutation.__module__ = "app.routers.users"


def route_allowed(uid):
    try:
        fence._check_pending(ordinary_mutation, (), {"user_id": uid})
        return True
    except HTTPException:
        return False


def disable(row):
    calls = []
    with factory() as db:
        conn = db.get(models.Connection, row[3])
        with patch.object(quota_manager.legacy_remote, "run", side_effect=lambda *a, **kw: (calls.append(kw), True)[1]):
            quota_manager._set_connection_enabled(db, conn, False)
        return conn.enabled, len(calls)


with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
        patch.dict(os.environ, {"UM_LOCK_DIR": _tmp.name}), patch.object(worker.MikrotikClient, "for_node", client), \
        patch.object(database, "SessionLocal", factory):
    legacy = fixture("legacy")
    with factory() as db:
        db.add(models.RadiusActiveSession(connection_id=legacy[4], session_id="legacy-session")); db.commit()
    router.sessions = ["legacy-session"]
    check("legacy precheck refused", run(legacy), "ppp_session_unverified")
    with factory() as db:
        check("refusal creates no operation", db.query(Operation).filter_by(purchase_id=legacy[0]).count(), 0)
        check("refusal creates no effective hold", block.purchase_pending(db, legacy[0]), False)
    check("refused precheck still permits customer mutation", route_allowed(legacy[1]), True)
    check("refused precheck still permits quota disable", disable(legacy), (False, 1))

    unreadable = fixture("unreadable")
    router.sessions, router.unreadable = [], True
    check("node read failure refused", run(unreadable), "ppp_sessions_unreadable")
    with factory() as db:
        check("node read failure creates no operation", db.query(Operation).filter_by(purchase_id=unreadable[0]).count(), 0)
    check("node read failure still permits quota disable", disable(unreadable), (False, 1))
    router.unreadable = False

    prepared = fixture("prepared")
    with factory() as db:
        opid = prepare(db, purchase_id=prepared[0], actor=db.get(models.AdminUser, prepared[2]))
        check("prepared-only crash has no effective hold", block.purchase_pending(db, prepared[0]), False)
    check("prepared-only crash permits ordinary mutation", route_allowed(prepared[1]), True)
    check("prepared-only crash permits quota disable", disable(prepared), (False, 1))
    with factory() as db:
        block_ppp(db, operation_id=opid, connection_id=prepared[4])
        check("a committed PPP block retains effective hold", block.purchase_pending(db, prepared[0]), True)
    check("real PPP block refuses ordinary mutation", route_allowed(prepared[1]), False)
    check("real PPP block retains worker ownership", disable(prepared), (True, 0))

    marked = fixture("marked", ppp=False)
    start_mutation = getattr(worker, "_start_mutation", None)
    check("durable pre-network mutation marker is available", callable(start_mutation), True)
    if start_mutation is not None:
        with factory() as db:
            marked_id = prepare(db, purchase_id=marked[0], actor=db.get(models.AdminUser, marked[2]))
            start_mutation(db, marked_id)  # crash before the network call
            check("crash after durable marker retains effective hold", block.purchase_pending(db, marked[0]), True)
        check("crash after marker refuses ordinary mutation", route_allowed(marked[1]), False)
        check("crash after marker retains worker ownership", disable(marked), (True, 0))

    uncertain = fixture("uncertain", ppp=False)
    with patch.object(worker, "_remote", side_effect=AdapterError("wg_refund_unreadable")):
        check("unknown first WG stop is pending", run(uncertain), "wg_refund_unreadable")
    with factory() as db:
        check("unknown WG stop retains effective hold", block.purchase_pending(db, uncertain[0]), True)
    check("unknown WG stop refuses ordinary mutation", route_allowed(uncertain[1]), False)
    with patch.object(settings, "reseller_cancellation_enabled", False):
        check("flag-off does not lift an uncertain WG hold", disable(uncertain), (True, 0))

        def endpoint_call(row, *, deleting=False):
            with factory() as db:
                actor = db.get(models.AdminUser, row[2])
                try:
                    if deleting:
                        users.delete_purchase(row[1], row[0], db=db, admin=actor, _confirm=None,
                                              _perm=None, x_idempotency_key=None)
                    else:
                        users.renew_purchase_endpoint(row[1], row[0],
                            schemas.PurchaseRenewRequest(add_gb=1, add_days=30), db=db, admin=actor,
                            _perm=None, x_idempotency_key=None)
                    return "completed"
                except HTTPException as exc:
                    return exc.status_code, exc.detail

        with factory() as db:
            db.get(models.AdminUser, uncertain[2]).is_superadmin = True
            db.commit()
        check("flag-off actual wrapped renewal refuses held service", endpoint_call(uncertain),
              (409, "reseller_cancellation_pending"))
        check("flag-off actual wrapped deletion refuses held service", endpoint_call(uncertain, deleting=True),
              (409, "reseller_cancellation_pending"))
        with factory() as db:
            p = db.get(models.Purchase, uncertain[0])
            check("flag-off refused routes neither renew nor delete", (p.reserved_duration_days, len(p.connections)),
                  (None, 1))

        ordinary = fixture("ordinary")
        with factory() as db:
            db.get(models.AdminUser, ordinary[2]).is_superadmin = True
            db.commit()
        check("flag-off unheld customer renewal remains available", endpoint_call(ordinary), "completed")
        with factory() as db:
            check("flag-off unheld customer renewal committed", db.get(models.Purchase, ordinary[0]).reserved_duration_days, 30)

        from sqlalchemy.exc import OperationalError
        with patch.object(block, "holding_operations", side_effect=OperationalError("test", {}, Exception("unavailable"))):
            check("unreadable hold state cannot bypass flag-off fence", endpoint_call(uncertain),
                  (503, "reseller_cancellation_state_unavailable"))
        with patch.object(database, "SessionLocal", side_effect=RuntimeError("test factory unavailable")):
            check("failed session factory cannot bypass flag-off fence", endpoint_call(uncertain),
                  (503, "reseller_cancellation_state_unavailable"))

if failures:
    raise SystemExit(f"{len(failures)} failed checks")
print("all checks passed")
