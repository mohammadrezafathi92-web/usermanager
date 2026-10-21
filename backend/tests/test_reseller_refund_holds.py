"""Unpaid cancellation must not start while anything says the customer paid
for THIS service, or other work owns it - and must resume, not re-judge, an
operation already started. Real SQLite, fake router, no network."""
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from app import deps, models
from app.config import settings
from app.database import Base, get_db
from app.models_provisioning import ProvisioningOperation
from app.models_reseller_refund import ResellerRefundOperation as Operation
from app.routers import users
from app.services import reseller_refund_worker as worker
from app.services.reseller_refund import bind_sale

failures = []


def check(label, got, expected):
    if got == expected:
        print(f"PASS  {label}")
    else:
        failures.append(label)
        print(f"FAIL  {label}\n        got:      {got!r}\n        expected: {expected!r}")


class Router:
    def __init__(self):
        self.peers = [{".id": "*1", "interface": "wg0", "comment": "peer", "public-key": "key",
                       "allowed-address": "10.0.0.2/32", "disabled": False, "tx": "30", "rx": "0"}]
        self.deleted = 0
        self.disabled_calls = 0
        self.fail_delete = False

    def get_system_resources(self):
        return {"uptime": "365d0h0m0s"}

    def list_peers(self, interface):
        return [dict(p) for p in self.peers]

    def set_peer_disabled(self, handle, disabled):
        self.disabled_calls += 1
        self.peers[0]["disabled"] = True

    def remove_peer(self, handle):
        if self.fail_delete:
            raise RuntimeError("node dropped the connection")
        self.deleted += 1
        self.peers.clear()

    def remove_simple_queue(self, name):
        pass


def bot_queue(path, rows):
    if os.path.exists(path):
        os.unlink(path)
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE pending_purchases (id INTEGER PRIMARY KEY, kind TEXT NOT NULL,
            target_username TEXT NOT NULL, status TEXT NOT NULL, renew_purchase_id INTEGER)""")
        conn.executemany("INSERT INTO pending_purchases (kind, target_username, status, renew_purchase_id) "
                         "VALUES (?,?,?,?)", rows)


with TemporaryDirectory() as directory:
    engine = create_engine(f"sqlite:///{directory}/holds.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    ProvisioningOperation.__table__.create(engine)
    with engine.begin() as conn:
        # Only the columns the hold reads; the real table is created by the
        # receipt-void schema in production.
        conn.execute(text("CREATE TABLE receipt_approvals (id INTEGER PRIMARY KEY, target_user_id_snapshot INTEGER, "
                          "target_purchase_id_snapshot INTEGER, state VARCHAR(20) NOT NULL)"))
    router = Router()

    @contextmanager
    def client(node):
        yield router

    with Session(engine) as db:
        actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", balance=0)
        node = models.Node(name="test-node", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0")
        package = models.Package(name="ماهانه", duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package]); db.flush()
        user = models.User(username="customer", owner_admin_id=actor.id, balance=456, purchase_count=2)
        db.add(user); db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=100, used_bytes=20,
                                   expire_at=datetime.utcnow() + timedelta(days=8))
        other = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=500, used_bytes=1,
                                expire_at=datetime.utcnow() + timedelta(days=30))
        db.add_all([purchase, other]); db.flush()
        connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
            type=models.ConnectionType.wireguard, wg_peer_name="peer", wg_public_key="key",
            wg_client_address="10.0.0.2/32", total_bytes=20, last_rx_bytes=20, last_tx_bytes=0)
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
        sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id,
                                  purchase_id=purchase.id)
        db.add_all([connection, debit, sale]); db.flush()
        bind_sale(db, purchase, debit, package, sale_entry=sale, purchase_count_delta=1)
        db.commit()
        pid, oid, uid, aid = purchase.id, other.id, user.id, actor.id

    queue = f"{directory}/bot_data.db"
    env = {"UM_LOCK_DIR": directory, "BOT_DB_PATH": queue}
    with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
         patch.dict(os.environ, env), patch.object(worker.MikrotikClient, "for_node", client):

        def availability():
            with Session(engine) as db:
                return worker.availability(db, db.get(models.Purchase, pid))

        def refused(label, code):
            with Session(engine) as db:
                try:
                    worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
                except HTTPException as exc:
                    check(label, (exc.detail, db.query(Operation).count(), router.disabled_calls), (code, 0, 0))
                else:
                    check(label, "executed", code)

        check("eligible with nothing holding it", availability(), None)

        print("--- a paid renewal queued on the service ---")
        with Session(engine) as db:
            p = db.get(models.Purchase, pid)
            p.reserved_quota_bytes, p.reserved_created_at = 100, datetime.utcnow()
            db.commit()
        refused("queued renewal: refused before any operation or remote stop", "reseller_paid_renewal_queued")
        with Session(engine) as db:
            p = db.get(models.Purchase, pid)
            p.reserved_quota_bytes = p.reserved_created_at = None
            u = db.get(models.User, uid)
            u.reserved_duration_days, u.reserved_created_at = 30, datetime.utcnow()
            db.commit()
        check("an old customer-level queued renewal holds it too", availability(), "reseller_paid_renewal_queued")
        with Session(engine) as db:
            u = db.get(models.User, uid)
            u.reserved_duration_days = u.reserved_created_at = None
            db.commit()

        print("--- a renewal receipt waiting in the bot ---")
        bot_queue(queue, [("renew", "customer", "pending", pid)])
        refused("receipt for THIS service waiting: refused as payment pending", "reseller_payment_pending")
        bot_queue(queue, [("renew", "customer", "processing", None)])
        check("an approval in progress for the customer's only-service renewal also holds", availability(),
              "reseller_payment_pending")
        bot_queue(queue, [("renew", "customer", "pending", oid), ("renew", "customer", "rejected", pid),
                          ("new", "customer", "pending", None)])
        check("receipts for another service, rejected ones and new purchases do not", availability(), None)
        with open(queue, "wb") as fh:
            fh.write(b"not a database")
        check("an unreadable payment queue is not taken as 'unpaid'", availability(),
              "reseller_payment_queue_unreadable")
        os.unlink(queue)

        print("--- a durable receipt approval or operation on the customer ---")
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO receipt_approvals (target_user_id_snapshot, target_purchase_id_snapshot, state) "
                              "VALUES (:u, :p, 'mutating')"), {"u": uid, "p": pid})
        check("an approval being applied: payment pending", availability(), "reseller_payment_pending")
        with engine.begin() as conn:
            conn.execute(text("UPDATE receipt_approvals SET state = 'failed'"))
        check("a finished approval does not", availability(), None)
        with Session(engine) as db:
            db.add(ProvisioningOperation(operation_type="renew_purchase", business_key="k1", request_hash="0" * 64,
                                         tenant_scope_key="shared", actor_kind="admin", intent="{}", state="prepared",
                                         forward_deadline=datetime.utcnow(), wallet_epoch_at_start=0,
                                         target_user_id=uid))
            db.commit()
        refused("an unfinished durable operation on the customer: refused", "reseller_operation_pending")
        with Session(engine) as db:
            db.query(ProvisioningOperation).update({"state": "completed", "completed_at": datetime.utcnow()})
            db.commit()
        check("once it completed, eligible again", availability(), None)

        print("--- a started operation is resumed, not re-judged ---")
        router.fail_delete = True
        with Session(engine) as db:
            try:
                worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
            except HTTPException as exc:
                detail = exc.detail
            else:
                detail = "completed"
        check("a failed remote delete is reported as the pending operation, with its reason",
              (detail["code"], detail["reason"]), ("reseller_cancellation_pending", "wg_refund_delete_unconfirmed"))
        with Session(engine) as db:
            check("...nothing credited, the service still in the database",
                  (db.get(models.AdminUser, aid).balance, db.get(models.Purchase, pid) is not None), (0, True))
        # A receipt arrives after the peer was already stopped: finishing is the
        # only consistent outcome - refusing would strand a half-removed service.
        bot_queue(queue, [("renew", "customer", "pending", pid)])
        router.fail_delete = False
        with Session(engine) as db:
            result = worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
        check("the resume completes the same operation", (result["state"], result["refund_amount"]), ("completed", 75000))

    app = FastAPI(); app.include_router(users.router)

    def database():
        with Session(engine) as db:
            yield db

    def caller():
        with Session(engine) as db:
            admin = db.get(models.AdminUser, aid)
            admin.group
            admin.permissions = "cancel_unpaid_services"
            return admin

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[deps.get_current_admin] = caller
    with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
         patch.dict(os.environ, env):
        with TestClient(app) as api:
            ops = api.get(f"/api/users/{uid}/unpaid-cancellations").json()
            check("the customer's operations are listed with their result",
                  [(o["purchase_id"], o["state"], o["refund_amount"], o["connections_removed"]) for o in ops],
                  [(pid, "completed", 75000, 1)])
            r = api.get(f"/api/users/{uid}/purchases/{oid}/unpaid-refund-preview").json()
            check("an unproven sale still shows its target and a reason, not a bare error",
                  (r["target"]["purchase_id"], r["target"]["package_name"], r["execution_available"],
                   r["unavailable_reason"]), (oid, "ماهانه", False, "reseller_charge_unproven"))
            check("...and says what is left alone", r["target"]["other_services"], 0)
    with Session(engine) as db:
        check("the other service and the customer wallet are untouched",
              (db.get(models.Purchase, oid) is not None, db.get(models.User, uid).balance), (True, 456))
    engine.dispose()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
