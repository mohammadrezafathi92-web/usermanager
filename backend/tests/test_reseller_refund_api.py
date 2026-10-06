"""HTTP permission/scope/password/confirmation, no hardware or network."""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from app import models, deps
from app.config import settings
from app.database import Base, get_db
from app.routers import users
from app.services.reseller_refund import bind_sale
from app.models_reseller_refund import ResellerRefundOperation as Operation

with TemporaryDirectory() as directory:
    engine = create_engine(f"sqlite:///{directory}/api.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        actor = models.AdminUser(username="seller", hashed_password="unused", role="seller", tree_path="/1/", permissions="")
        node = models.Node(name="fake", type=models.NodeType.mikrotik)
        package = models.Package(name="test", duration_days=10, price=100000, quota_gb=1)
        db.add_all([actor, node, package]); db.flush()
        user = models.User(username="customer", owner_admin_id=actor.id)
        db.add(user); db.flush()
        purchase = models.Purchase(user_id=user.id, quota_bytes=100, expire_at=datetime.utcnow() + timedelta(days=8))
        db.add(purchase); db.flush()
        connection = models.Connection(user_id=user.id, node_id=node.id, purchase_id=purchase.id,
                                       type=models.ConnectionType.wireguard)
        debit = models.LedgerEntry(kind="admin_credit_spend", amount=100000, admin_id=actor.id)
        db.add_all([connection, debit]); db.flush()
        bind_sale(db, purchase, debit, package); db.commit()
        aid, uid, pid = actor.id, user.id, purchase.id
    app = FastAPI(); app.include_router(users.router)
    def database():
        with Session(engine) as db:
            yield db
    allowed = False
    password_ok = False
    def caller():
        with Session(engine) as db:
            actor = db.get(models.AdminUser, aid)
            actor.group  # load the nullable relationship before detaching
            if allowed:
                actor.permissions = "cancel_unpaid_services,edit_users"
            return actor
    def password():
        if not password_ok:
            raise HTTPException(403, "password_required")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[deps.get_current_admin] = caller
    app.dependency_overrides[deps.require_confirm_password] = password
    with TestClient(app) as client:
        path = f"/api/users/{uid}/purchases/{pid}"
        assert client.get(path + "/unpaid-refund-preview").status_code == 403
        allowed = True
        response = client.get(path + "/unpaid-refund-preview")
        assert response.status_code == 200, response.text
        assert response.json()["execution_available"] is False
        assert client.get(f"/api/users/{uid + 999}/purchases/{pid}/unpaid-refund-preview").status_code == 404
        assert client.post(path + "/cancel-unpaid", json={"confirm_unpaid": True}).status_code == 403
        password_ok = True
        assert client.post(path + "/cancel-unpaid", json={"confirm_unpaid": False}).status_code == 422
        assert client.post(path + "/cancel-unpaid", json={"confirm_unpaid": True}).status_code == 503
        with patch.object(users.reseller_refund_worker, "execute", return_value={"state": "completed", "refund_amount": 123}) as call:
            assert client.post(path + "/cancel-unpaid", json={"confirm_unpaid": True}).json()["refund_amount"] == 123
            assert call.call_count == 1
    with Session(engine) as db:
        assert db.query(Operation).count() == 0
        assert db.get(models.AdminUser, aid).balance == 0
        db.add(Operation(basis_id=1, purchase_id=pid, user_id=uid, requested_by=aid,
                         state="pending", baseline_used_bytes=0))
        db.commit()
    with patch.object(settings, "reseller_cancellation_enabled", True), \
         patch.dict(os.environ, {"UM_LOCK_DIR": directory}), \
         patch("app.database.SessionLocal", sessionmaker(bind=engine)):
        with TestClient(app) as client:
            response = client.put(f"/api/users/{uid}", json={"full_name": "should not change"})
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == "reseller_cancellation_pending"
        # PanelBridge calls module functions directly: the same guard applies.
        with Session(engine) as db:
            try:
                users.update_user(user_id=uid, payload=users.schemas.UserUpdate(full_name="blocked"),
                                  db=db, admin=db.get(models.AdminUser, aid))
            except HTTPException as exc:
                assert exc.detail == "reseller_cancellation_pending"
            else:
                raise AssertionError("direct bridge bypassed pending-operation guard")
            assert db.get(models.User, uid).full_name is None
    engine.dispose()
print("PASS: scoped preview, explicit seller permission, password, confirmation, disabled execution")
