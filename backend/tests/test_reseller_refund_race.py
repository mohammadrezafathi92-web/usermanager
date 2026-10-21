"""Payment or renewal racing an unpaid cancellation, with fixed interleavings.

  A. a paid renewal commits AFTER the availability check, BEFORE the
     operation row exists -> refused inside the prepare transaction;
  B. same window, a durable provisioning operation -> refused;
  C. a renewal while the operation is pending -> refused by the fence;
  D. a renewal started while the cancellation holds its exclusive barrier
     (mid remote delete) waits, and then finds the service gone - it never
     extends a service being deleted;
  E. resume after a failed delete continues the SAME operation: one remote
     delete, one refund, and a further retry changes nothing;
  F. SQLite reuses the deleted service's id: the settled operation and its
     basis retire it, so the next service under that id is judged alone,
     and a retry of the old request (its service key) gets the old result
     while the new service stays untouched;
  G. a pending operation whose service was removed outside it and whose
     id went to a new service is never resumed onto that service.

Real SQLite, real file locks, fake router. No network."""
import os
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from app import models, schemas
from app.config import settings
from app.database import Base
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
        self.peers = {}
        self.deleted = 0
        self.disabled = 0
        self.fail_delete = False
        self.in_delete = threading.Event()
        self.release = None

    def add(self, handle, name, key, address):
        self.peers[handle] = {".id": handle, "interface": "wg0", "comment": name, "public-key": key,
                              "allowed-address": address, "disabled": False, "tx": "30", "rx": "0"}

    def get_system_resources(self):
        return {"uptime": "365d0h0m0s"}

    def list_peers(self, interface):
        return [dict(p) for p in self.peers.values()]

    def set_peer_disabled(self, handle, disabled):
        self.disabled += 1
        self.peers[handle]["disabled"] = True

    def remove_peer(self, handle):
        self.in_delete.set()
        if self.release is not None:
            self.release.wait(20)
        if self.fail_delete:
            self.fail_delete = False
            raise RuntimeError("node dropped the connection")
        self.deleted += 1
        self.peers.pop(handle)

    def remove_simple_queue(self, name):
        pass


def seed(engine, router, n):
    with Session(engine) as db:
        actor = models.AdminUser(username=f"seller{n}", hashed_password="unused", role="seller",
                                 tree_path="/1/", balance=0, permissions="cancel_unpaid_services,edit_users")
        node = models.Node(name=f"node{n}", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0")
        package = models.Package(name="test", duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package]); db.flush()
        user = models.User(username=f"customer{n}", owner_admin_id=actor.id, balance=456, purchase_count=1)
        db.add(user); db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=100, used_bytes=20,
                                   expire_at=datetime.utcnow() + timedelta(days=8))
        db.add(purchase); db.flush()
        router.add(f"*{n}", f"peer{n}", f"key{n}", f"10.0.0.{n}/32")
        db.add(models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
            type=models.ConnectionType.wireguard, wg_peer_name=f"peer{n}", wg_public_key=f"key{n}",
            wg_client_address=f"10.0.0.{n}/32", total_bytes=20, last_rx_bytes=20, last_tx_bytes=0))
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
        sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id,
                                  purchase_id=purchase.id)
        db.add_all([debit, sale]); db.flush()
        bind_sale(db, purchase, debit, package, sale_entry=sale, purchase_count_delta=1)
        db.commit()
        return actor.id, user.id, purchase.id


def run_cancel(engine, aid, pid):
    with Session(engine) as db:
        try:
            return worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid))
        except HTTPException as exc:
            return exc.detail


def renew(engine, aid, uid, pid):
    """The panel's own per-service renewal, called as PanelBridge calls it
    (the module function, which the cancellation fence wraps)."""
    with Session(engine) as db:
        try:
            users.renew_purchase_endpoint(user_id=uid, purchase_id=pid,
                                          payload=schemas.PurchaseRenewRequest(add_gb=1, add_days=30),
                                          db=db, admin=db.get(models.AdminUser, aid), _perm=None,
                                          x_idempotency_key=None)
            return "renewed"
        except HTTPException as exc:
            return exc.detail


def inject_before_prepare(effect):
    """Run `effect` (in its own session and commit) exactly between the
    worker's availability check and prepare(): preview() runs in between."""
    original = worker.preview
    fired = []

    def wrapped(db, purchase, **kw):
        if not fired:
            fired.append(True)
            effect()
        return original(db, purchase, **kw)
    return patch.object(worker, "preview", wrapped)


with TemporaryDirectory() as directory:
    engine = create_engine(f"sqlite:///{directory}/race.db", connect_args={"check_same_thread": False, "timeout": 30})
    Base.metadata.create_all(engine)
    ProvisioningOperation.__table__.create(engine)
    router = Router()

    @contextmanager
    def client(node):
        yield router

    with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
         patch.dict(os.environ, {"UM_LOCK_DIR": directory, "BOT_DB_PATH": f"{directory}/none.db"}), \
         patch.object(worker.MikrotikClient, "for_node", client), \
         patch("app.database.SessionLocal", sessionmaker(bind=engine)):

        print("--- A: a paid renewal commits between the check and the operation ---")
        aid, uid, pid = seed(engine, router, 1)

        def queue_renewal():
            with Session(engine) as other:
                p = other.get(models.Purchase, pid)
                p.reserved_quota_bytes, p.reserved_duration_days, p.reserved_created_at = 100, 10, datetime.utcnow()
                other.commit()
        with inject_before_prepare(queue_renewal):
            result = run_cancel(engine, aid, pid)
        with Session(engine) as db:
            check("refused inside prepare: no operation row, no remote stop, no credit",
                  (result, db.query(Operation).filter_by(purchase_id=pid).count(), router.disabled,
                   db.get(models.AdminUser, aid).balance), ("reseller_paid_renewal_queued", 0, 0, 0))
            check("...the paid renewal and the service are intact",
                  (db.get(models.Purchase, pid).reserved_quota_bytes, len(router.peers)), (100, 1))

        print("--- B: a durable operation commits in the same window ---")
        aid, uid, pid = seed(engine, router, 2)

        def start_operation():
            with Session(engine) as other:
                other.add(ProvisioningOperation(operation_type="renew_purchase", business_key=f"race-{pid}",
                    request_hash="0" * 64, tenant_scope_key="shared", actor_kind="bot", intent="{}",
                    state="prepared", forward_deadline=datetime.utcnow(), wallet_epoch_at_start=0,
                    target_user_id=uid))
                other.commit()
        with inject_before_prepare(start_operation):
            result = run_cancel(engine, aid, pid)
        with Session(engine) as db:
            check("refused: operation pending, nothing created or stopped",
                  (result, db.query(Operation).filter_by(purchase_id=pid).count(), router.disabled),
                  ("reseller_operation_pending", 0, 0))

        print("--- C/E: renewal while pending; resume continues the same operation ---")
        aid, uid, pid = seed(engine, router, 3)
        with Session(engine) as db:
            old_key = db.get(models.Purchase, pid).history_key
        router.fail_delete = True
        first = run_cancel(engine, aid, pid)
        check("a failed remote delete leaves the operation pending",
              (first["code"], first["reason"]), ("reseller_cancellation_pending", "wg_refund_delete_unconfirmed"))
        with Session(engine) as db:
            before = db.get(models.Purchase, pid).expire_at
        check("a renewal while pending is refused by the fence", renew(engine, aid, uid, pid),
              "reseller_cancellation_pending")
        with Session(engine) as db:
            check("...and changed nothing", db.get(models.Purchase, pid).expire_at, before)
            op_id = db.query(Operation).filter_by(purchase_id=pid).one().id
        done = run_cancel(engine, aid, pid)
        again = run_cancel(engine, aid, pid)
        with Session(engine) as db:
            refunds = db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund", admin_id=aid).count()
            check("resume: the same operation completes once", (done["operation_id"], done["state"]), (op_id, "completed"))
            check("one remote delete, one refund, retry returns the same result",
                  (router.deleted, refunds, db.get(models.AdminUser, aid).balance, again), (1, 1, 75000, done))

        print("--- D: a renewal racing the remote delete waits, then finds the service gone ---")
        previous_pid, previous_op, previous_aid, previous_uid = pid, op_id, aid, uid
        aid, uid, pid = seed(engine, router, 4)
        check("SQLite reused the deleted service's id for a new, unrelated service", pid, previous_pid)
        with Session(engine) as db:
            old_op = db.get(Operation, previous_op)
            check("...the settled operation retired that id, so the new sale has its own basis",
                  (old_op.purchase_id, old_op.retired_purchase_id), (-previous_op, previous_pid))
            new = db.get(models.Purchase, pid)
            check("...and the old result does not answer for the new service",
                  worker.availability(db, new), None)
            peers_before, deleted_before = dict(router.peers), router.deleted
        with Session(engine) as db:
            retry = worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, previous_aid),
                                   purchase_key=old_key, user_id=previous_uid)
        with Session(engine) as db:
            check("a retry of the OLD request (its service key) returns that operation's stored result",
                  (retry["operation_id"], retry["state"], retry["refund_amount"]), (previous_op, "completed", 75000))
            check("...and leaves the new service under the reused id untouched",
                  (db.get(models.Purchase, pid) is not None, router.peers == peers_before, router.deleted,
                   db.query(Operation).filter_by(purchase_id=pid).count(),
                   db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count()),
                  (True, True, deleted_before, 0, 1))
            new_key = db.get(models.Purchase, pid).history_key
        check("the reused id carries a different service key", new_key != old_key, True)
        router.in_delete.clear()
        router.release = threading.Event()
        out = {}
        cancel = threading.Thread(target=lambda: out.setdefault("cancel", run_cancel(engine, aid, pid)))
        cancel.start()
        check("the cancellation reached the remote delete", router.in_delete.wait(10), True)
        renewal = threading.Thread(target=lambda: out.setdefault("renew", renew(engine, aid, uid, pid)))
        renewal.start()
        time.sleep(1.0)
        check("...the renewal is held behind the cancellation's barrier", "renew" in out, False)
        router.release.set()
        cancel.join(30); renewal.join(30)
        router.release = None
        check("the cancellation completed", out["cancel"]["state"], "completed")
        check("the renewal then refused: the service no longer exists", out["renew"], "خرید پیدا نشد")
        with Session(engine) as db:
            check("no renewal applied to anything, one refund", (db.get(models.Purchase, pid),
                  db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund", admin_id=aid).count()), (None, 1))

        print("--- G: a pending operation whose id another service took is never resumed onto it ---")
        aid, uid, pid = seed(engine, router, 5)
        router.fail_delete = True
        first = run_cancel(engine, aid, pid)
        check("pending after a failed delete", first["code"], "reseller_cancellation_pending")
        with Session(engine) as db:
            key5 = db.get(models.Purchase, pid).history_key
            # Removed outside the cancellation (e.g. by hand in the database)
            # and the id handed to a new service of the same customer.
            for c in db.get(models.Purchase, pid).connections:
                db.delete(c)
            db.delete(db.get(models.Purchase, pid))
            db.commit()
            db.add(models.Purchase(id=pid, user_id=uid, quota_bytes=500, used_bytes=0,
                                   expire_at=datetime.utcnow() + timedelta(days=30)))
            db.commit()
            disabled_before = router.disabled
        resumed = run_cancel(engine, aid, pid)
        check("resume refuses: the id now names another service", resumed, "reseller_service_changed")
        with Session(engine) as db:
            try:
                worker.execute(db, purchase_id=pid, actor=db.get(models.AdminUser, aid), purchase_key=key5, user_id=uid)
                keyed = "executed"
            except HTTPException as exc:
                keyed = exc.detail
            check("...also when retried with the old service key", keyed, "reseller_service_changed")
            fresh = db.get(models.Purchase, pid)
            check("...the new service is untouched, nothing stopped, nothing credited",
                  (fresh.quota_bytes, router.disabled, db.get(models.AdminUser, aid).balance), (500, disabled_before, 0))
            check("...and the old operation stays pending for the record",
                  db.query(Operation).filter_by(purchase_key=key5).one().state, "pending")
    engine.dispose()

print("\n" + "=" * 60)
if failures:
    print(f"{len(failures)} FAILED: " + ", ".join(failures))
    sys.exit(1)
print("all checks passed")
