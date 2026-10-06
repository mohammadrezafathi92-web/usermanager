"""Real SQLite transaction + fake router, no network or server changes."""
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app import models
from app.database import Base
from app.config import settings
from app.services import reseller_refund_worker as worker, accounting
from app.services.reseller_refund import bind_sale
from app.models_reseller_refund import ResellerRefundOperation as Operation


class Router:
    def __init__(self):
        self.offline = False
        self.peers = [{".id": "*1", "interface": "wg0", "comment": "peer", "public-key": "key",
                       "allowed-address": "10.0.0.2/32", "disabled": False, "tx": "30", "rx": "0"}]
        self.deleted = 0
        self.uptime = "365d0h0m0s"
    def get_system_resources(self):
        return {"uptime": self.uptime}
    def list_peers(self, interface):
        if self.offline:
            raise RuntimeError("offline")
        return [dict(p) for p in self.peers]
    def set_peer_disabled(self, handle, disabled):
        assert handle == "*1" and disabled
        self.peers[0]["disabled"] = True
    def remove_peer(self, handle):
        assert handle == "*1"
        self.deleted += 1
        self.peers.clear()
    def remove_simple_queue(self, name):
        assert name == "um-speed-peer"


def run(directory):
    engine = create_engine(f"sqlite:///{directory}/worker.db")
    Base.metadata.create_all(engine)
    router = Router()
    @contextmanager
    def client(node):
        yield router
    with Session(engine) as db:
        actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", balance=0)
        node = models.Node(name="fake", type=models.NodeType.mikrotik, mt_wireguard_interface="wg0")
        package = models.Package(name="test", duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package]); db.flush()
        user = models.User(username="customer", owner_admin_id=actor.id, balance=456, purchase_count=1)
        db.add(user); db.flush()
        purchase = models.Purchase(user_id=user.id, package_id=package.id, quota_bytes=100, used_bytes=20,
                                   expire_at=datetime.utcnow() + timedelta(days=8))
        db.add(purchase); db.flush()
        connection = models.Connection(user_id=user.id, purchase_id=purchase.id, node_id=node.id,
            type=models.ConnectionType.wireguard, wg_peer_name="peer", wg_public_key="key",
            wg_client_address="10.0.0.2/32", total_bytes=20, last_rx_bytes=20, last_tx_bytes=0)
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
        sale = models.LedgerEntry(kind="sale_new", amount=120000, admin_id=actor.id, user_id=user.id,
                                 purchase_id=purchase.id)
        db.add_all([connection, debit, sale]); db.flush()
        bind_sale(db, purchase, debit, package, sale_entry=sale, purchase_count_delta=1)
        db.commit()
        pid, uid, aid, sid = purchase.id, user.id, actor.id, sale.id
        assert worker.availability(db, purchase) == "reseller_cancellation_disabled"
        with patch.object(settings, "reseller_cancellation_enabled", True), patch.object(worker.sys, "platform", "linux"), \
             patch.dict(os.environ, {"UM_LOCK_DIR": directory}), patch.object(worker.MikrotikClient, "for_node", client):
            assert worker.availability(db, purchase) is None
            router.offline = True
            try:
                worker.execute(db, purchase_id=pid, actor=actor)
            except HTTPException as exc:
                assert exc.detail["code"] == "reseller_cancellation_pending"
            else:
                raise AssertionError("offline node accepted")
            assert db.get(models.AdminUser, aid).balance == 0
            assert db.get(models.Purchase, pid) is not None
            operation = db.query(Operation).one()
            assert operation.state == "pending" and router.deleted == 0
            router.offline = False
            router.uptime = "0s"
            try:
                worker.execute(db, purchase_id=pid, actor=actor)
            except HTTPException as exc:
                assert exc.detail["reason"] == "wg_usage_continuity_unverified", exc.detail
            else:
                raise AssertionError("rebooted node counters accepted")
            assert router.deleted == 0 and db.get(models.AdminUser, aid).balance == 0
            router.uptime = "365d0h0m0s"
            result = worker.execute(db, purchase_id=pid, actor=actor)
            assert result["refund_amount"] == 75000, result
            assert worker.execute(db, purchase_id=pid, actor=actor) == result
            assert router.deleted == 1
            assert db.get(models.Purchase, pid) is None
            assert db.get(models.User, uid).balance == 456
            assert db.get(models.User, uid).purchase_count == 0
            assert db.get(models.AdminUser, aid).balance == 75000
            assert db.get(models.LedgerEntry, sid).voided_at is not None
            assert db.query(models.LedgerEntry).filter_by(kind="admin_credit_refund").count() == 1
            assert accounting.summary(db, actor)["sales_total"] == 0
    engine.dispose()


with TemporaryDirectory() as directory:
    run(directory)
assert worker._uptime_seconds("1w2d03:04:05") == 604800 + 172800 + 11045
assert worker._uptime_seconds("2d3h4m5s") == 172800 + 11045
assert worker._uptime_seconds("03:04:05") == 11045
print("PASS: offline recovery, verified remote stop, atomic accounting, single refund, unchanged customer wallet")
